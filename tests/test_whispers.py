"""EarningsWhispers as the single source of earnings numbers.

Offline: every case reads a response recorded from the live API into
`tests/fixtures/whispers/` on 2026-08-05. Nothing here touches the network.

Two failure modes are worth more than the rest and are pinned first, because
either one silently corrupts a BUY:

- the API reports revenue in MILLIONS while every contract in this system is
  in dollars, so a missing conversion is a 1,000,000x beat;
- the API answers with the company's PREVIOUS quarter until it ingests the new
  one, so a print read too early compares this quarter's consensus against
  last quarter's actual.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from hawkeye.marketdata.whispers import (
    WhispersSource,
    WhispersUnavailable,
    parse_details,
    read_consensus,
    read_guidance,
)

FIXTURES = Path(__file__).parent / "fixtures" / "whispers"
ET = ZoneInfo("America/New_York")


def payload(ticker: str) -> dict:
    return json.loads((FIXTURES / f"{ticker}.json").read_text(
        encoding="utf-8", errors="replace"))


def record(ticker: str):
    return parse_details(payload(ticker))


# -- numbers ---------------------------------------------------------------

def test_revenue_is_converted_from_millions_into_dollars():
    # ADM's release says $22.68 billion; the API field says 22681.0.
    assert record("ADM").revenue_actual == pytest.approx(22_681_000_000.0)


def test_revenue_consensus_is_converted_too():
    assert record("ADM").revenue_consensus == pytest.approx(22_380_000_000.0)


def test_eps_is_taken_as_published_without_scaling():
    row = record("ADM")
    assert row.eps_actual == pytest.approx(1.84)
    assert row.eps_consensus == pytest.approx(1.42)


def test_absent_whisper_number_reads_as_missing_not_as_999():
    # 999 is the sentinel the API uses for "no whisper number".
    assert record("AATC").whisper is None


def test_an_absent_consensus_reads_as_missing_not_as_a_999_dollar_bar():
    """The 999 sentinel is not confined to the whisper field. 8 of the 47
    recorded companies carry `estimate: 999.0`, and they are exactly the 8
    with no high/low pair — the ones nobody published a consensus for.

    Read as a figure it is a $999 EPS bar, so a company that reported $0.30
    against no consensus at all becomes a 100% MISS. The screen drops it
    either way, which is why this hid: the name disappears for a plausible
    reason, and the drop record then says the company failed rather than
    that we had nothing to measure it against (invariant 6)."""
    row = record("AATC")
    assert row.eps_consensus is None
    assert "eps_consensus_missing" in row.gaps


def test_999_is_not_a_sentinel_in_the_revenue_fields():
    """Revenue arrives in MILLIONS, so 999 there is $999m — an ordinary
    mid-cap figure, not a marker. Stripping it would turn a real consensus
    into a missing one and send the whole print to the calendar for nothing.
    A missing revenue consensus is spelled `null` in this feed."""
    row = parse_details({"ticker": "X", "revenue": 999.0,
                         "revenueEstimate": 999.0})
    assert row.revenue_actual == pytest.approx(999_000_000.0)
    assert row.revenue_consensus == pytest.approx(999_000_000.0)


def test_a_sentinel_high_low_pair_does_not_become_a_range():
    row = record("ABTC")
    assert row.eps_consensus is None
    assert row.eps_consensus_high is None
    assert row.eps_consensus_low is None


def test_a_real_whisper_number_survives():
    assert record("ADM").whisper == pytest.approx(1.47)


def test_absent_revenue_consensus_is_none_not_zero():
    # A zero here would read as a beat of infinite size downstream.
    assert record("AATC").revenue_consensus is None


# -- the vendor's own numbers, kept for measurement and never for judgment --
#
# EW publishes its own surprise, and it is NOT always the same comparison this
# system makes: where a whisper number exists the vendor measures against THAT
# instead of consensus. AMD 2026-08-04 came back as -1.78% ("missed
# expectations", against the $1.69 whisper) while actual-vs-consensus is
# +2.47%. Both readings are recorded; only ours decides anything.

def test_the_vendor_surprise_is_recorded_as_a_percentage():
    # The field arrives as a ratio (0.0503), and every other percentage in
    # this system is a percentage.
    assert record("AME").vendor_eps_surprise_pct == pytest.approx(5.025, abs=1e-3)
    assert record("AME").vendor_revenue_surprise_pct == pytest.approx(4.306,
                                                                      abs=1e-3)


def test_the_vendor_reading_is_kept_even_when_it_contradicts_ours():
    row = record("AMD")
    assert row.vendor_eps_surprise_pct < 0          # the vendor says "missed"
    assert row.eps_actual > row.eps_consensus       # consensus says otherwise
    assert row.whisper == pytest.approx(1.69)       # what the vendor measured


def test_the_vendor_reading_says_which_bar_it_used():
    # Without this the two readings cannot be compared after the fact: the
    # difference is entirely explained by which bar the vendor chose.
    assert record("AMD").vendor_surprise_basis == "whisper"
    assert record("AME").vendor_surprise_basis == "consensus"


def test_a_vendor_surprise_that_is_absent_stays_absent():
    hollow = parse_details({"ticker": "X"})
    assert hollow.vendor_eps_surprise_pct is None
    assert hollow.vendor_surprise_basis == ""


# -- which quarter this is -------------------------------------------------

def test_fiscal_quarter_is_derived_from_the_quarter_end_month():
    assert record("ADM").fiscal_quarter == "2026-Q2"


def test_fiscal_quarter_follows_the_companys_own_year_end_not_the_calendar():
    # NVDA's fiscal year ends in January, so a quarter ending April 2026 is
    # fiscal 2027 Q1 — the calendar quarter (2026-Q2) would be wrong.
    row = record("NVDA")
    assert row.quarter_end == date(2026, 4, 30)
    assert row.fiscal_quarter == "2027-Q1"


def test_a_missing_quarter_reference_is_reported_rather_than_guessed():
    row = record("ANDE")
    assert row.quarter_end is None
    assert row.fiscal_quarter is None
    assert "quarter_reference_missing" in row.gaps


# -- when it was announced, and whether it is this print at all ------------

def test_announcement_time_is_read_as_us_eastern():
    assert record("ADM").announced_at == datetime(2026, 8, 4, 6, 0, tzinfo=ET)


def test_a_record_covers_the_print_it_was_read_for():
    assert record("ADM").covers(date(2026, 8, 4)) is True


def test_a_record_from_the_previous_quarter_does_not_cover_todays_print():
    # ACA was on the 2026-08-05 calendar; the API still held its 2026-04-30
    # print. Believing this row would judge one quarter's actual against
    # another quarter's consensus.
    row = record("ACA")
    assert row.covers(date(2026, 8, 5)) is False
    assert row.staleness_reason(date(2026, 8, 5)) == "previous_quarter"


def test_the_covering_window_allows_a_calendar_off_by_one_day():
    # Vendors disagree by a day on after-hours prints; a same-quarter record
    # one day off is the same print, not a stale one.
    assert record("ADM").covers(date(2026, 8, 5)) is True


# -- guidance --------------------------------------------------------------
#
# The prose puts three ranges of numbers next to each other — what the company
# now expects, what it expected BEFORE, and what analysts expect — and only the
# first is guidance. Every test below that names a decoy is guarding against
# reading one of the other two as the company's own words.

def test_next_quarter_eps_guidance_is_read_with_the_quarter_it_belongs_to():
    out = read_guidance(record("AME"))
    assert out.reading.period == "2026-Q3"
    assert out.reading.eps_low == pytest.approx(1.85)
    assert out.reading.eps_high == pytest.approx(1.87)


def test_full_year_guidance_is_kept_separately_from_the_quarterly_one():
    # AME guided both. They must not be merged: the quarterly figure is the
    # only one comparable with next quarter's consensus.
    out = read_guidance(record("AME"))
    assert out.full_year.period == "FY2026"
    assert out.full_year.eps_low == pytest.approx(8.20)
    assert out.full_year.eps_high == pytest.approx(8.30)


def test_full_year_only_guidance_leaves_the_quarterly_reading_empty():
    # ADM guided the year and not the quarter. Returning $5.15-$5.60 as "the
    # guidance" would later be compared against a QUARTERLY consensus of about
    # $1.20 and read as a 4x beat.
    out = read_guidance(record("ADM"))
    assert out.reading is None
    assert out.full_year.eps_low == pytest.approx(5.15)
    assert out.reason == "full_year_only"


def test_the_companys_previous_guidance_is_never_read_as_its_guidance():
    out = read_guidance(record("ADM"))
    assert out.full_year.eps_low != pytest.approx(4.15)
    assert "previous guidance" not in out.excerpt


def test_the_analyst_consensus_in_the_same_sentence_is_not_read_as_guidance():
    out = read_guidance(record("AMD"))
    assert out.reading.revenue_low == pytest.approx(12_700_000_000.0)
    assert out.reading.revenue_high == pytest.approx(13_300_000_000.0)
    assert "current consensus" not in out.excerpt


def test_revenue_guidance_in_millions_becomes_dollars():
    out = read_guidance(record("ADTN"))
    assert out.reading.revenue_low == pytest.approx(275_000_000.0)
    assert out.reading.revenue_high == pytest.approx(295_000_000.0)


def test_a_clause_that_guides_both_eps_and_revenue_yields_both():
    out = read_guidance(record("ALAB"))
    assert out.reading.eps_low == pytest.approx(1.16)
    assert out.reading.eps_high == pytest.approx(1.21)
    assert out.reading.revenue_low == pytest.approx(540_000_000.0)
    assert out.reading.revenue_high == pytest.approx(560_000_000.0)


def test_a_single_approximate_figure_is_a_range_of_one_value():
    out = read_guidance(record("ADNT"))
    assert out.full_year.revenue_low == pytest.approx(14_800_000_000.0)
    assert out.full_year.revenue_high == pytest.approx(14_800_000_000.0)


def test_an_open_ended_floor_is_refused_rather_than_read_as_a_midpoint():
    # ALGT: "2026 earnings of more than $6.00 per share". Treating $6.00 as
    # the midpoint would understate guidance the company deliberately left
    # open, and treating it as a range needs a top nobody published.
    out = read_guidance(record("ALGT"))
    assert out.full_year is None
    assert "open_ended" in out.reason


def test_a_range_that_is_not_numeric_is_refused():
    # ALGT again: "third quarter results to range from a loss of $1.00 per
    # share to breakeven".
    out = read_guidance(record("ALGT"))
    assert out.reading is None


def test_a_company_that_published_no_guidance_says_so_explicitly():
    out = read_guidance(record("ACEL"))
    assert out.reading is None and out.full_year is None
    assert out.reason == "no_guidance_clause"
    assert out.excerpt == ""


# -- the forms of "we expect" ----------------------------------------------
#
# The clause matcher used to accept exactly one wording, "the company said it
# expects", and four of the 47 recorded names (ADEA ADV AHCO ALB) were
# therefore recorded as having guided NOTHING while stating a full-year
# revenue range in plain numbers. Nothing about them is hard to read — the
# sentence just starts differently (measured 2026-08-10).

def test_a_company_that_continues_to_expect_is_still_guiding():
    # ADEA: "The company said it continues to expect 2026 revenue of $395.0
    # million to $435.0 million."
    out = read_guidance(record("ADEA"))
    assert out.full_year is not None
    assert out.full_year.period == "FY2026"
    assert out.full_year.revenue_low == pytest.approx(395_000_000.0)
    assert out.full_year.revenue_high == pytest.approx(435_000_000.0)


def test_a_clause_interrupted_before_the_verb_is_still_read():
    # AHCO: "The company said, with the divestiture in its Diabetes Health
    # business, it now expects 2026 revenue of $2.85 billion to $2.89
    # billion." The previous guidance ($3.45-$3.52 billion) sits in the very
    # next sentence and must not be the range that gets read.
    out = read_guidance(record("AHCO"))
    assert out.full_year is not None
    assert out.full_year.revenue_low == pytest.approx(2_850_000_000.0)
    assert out.full_year.revenue_high == pytest.approx(2_890_000_000.0)
    assert "previous guidance" not in out.excerpt


def test_an_inverted_range_is_refused_rather_than_averaged():
    # ADV states "2026 revenue of $3.54 billion to $2.67 billion" — a top
    # below its own floor, i.e. the vendor mistyped one of the two. Averaging
    # them yields $3.10 billion, a figure nobody published, and it would be
    # compared against a real consensus as if the company had said it.
    out = read_guidance(record("ADV"))
    assert out.full_year is None
    assert "range_inverted" in out.reason


def test_the_excerpt_is_the_sentence_the_numbers_were_read_from():
    out = read_guidance(record("ALSN"))
    assert out.excerpt.startswith("The company")
    assert "2026 revenue of $5.80 billion to $6.00 billion" in out.excerpt


# -- the analyst consensus in the same prose -------------------------------
#
# The full-year yardstick is in the SAME summary string the guidance came from,
# so reading it costs no request. Without it a company that guided the year —
# 13 of the 47 recorded names — is recorded as having guided nothing, because
# the only yardstick this system captured was next quarter's (EW移行 §5).

def test_the_full_year_eps_consensus_is_read_from_the_same_summary():
    out = read_consensus(record("AME"))
    assert out.full_year_eps == pytest.approx(8.14)
    assert out.full_year_period == "FY2026"


def test_the_full_year_revenue_consensus_is_converted_into_dollars():
    # ACA states it in billions, and every contract here is in dollars.
    out = read_consensus(record("ACA"))
    assert out.full_year_revenue == pytest.approx(3_020_000_000.0)
    assert out.full_year_eps is None


def test_one_clause_can_state_both_a_full_year_eps_and_revenue_consensus():
    # AMRC: "$1.13 per share on revenue of $2.08 billion for the year ending".
    out = read_consensus(record("AMRC"))
    assert out.full_year_eps == pytest.approx(1.13)
    assert out.full_year_revenue == pytest.approx(2_080_000_000.0)


def test_the_quarterly_figure_in_the_same_sentence_is_never_read_as_the_year():
    # AGNT states both: $1.36 billion for the quarter, $5.02 billion for the
    # year. Taking the first would put a quarter's bar under a year's guidance.
    out = read_consensus(record("AGNT"))
    assert out.full_year_revenue == pytest.approx(5_020_000_000.0)
    assert out.next_quarter_revenue == pytest.approx(1_360_000_000.0)


def test_the_companys_own_guidance_is_never_read_as_the_analyst_consensus():
    # ADMA's sentence carries three revenue figures: the company's new
    # guidance ($530-560m), its previous guidance ($635m), and the consensus
    # ($611.67m). Only the last is a yardstick.
    out = read_consensus(record("ADMA"))
    assert out.full_year_revenue == pytest.approx(611_670_000.0)


def test_a_non_december_fiscal_year_keeps_the_companys_own_year_end():
    # ADNT's year ends in September. The label has to follow the company, not
    # the calendar, or its guidance is measured against a different year.
    out = read_consensus(record("ADNT"))
    assert out.full_year_period == "FY2026"
    assert out.full_year_revenue == pytest.approx(14_580_000_000.0)


def test_a_summary_that_names_only_a_quarter_has_no_full_year_yardstick():
    # AMD's consensus sentence stops at the quarter. Absence is named, never
    # a bare None (invariant 6).
    out = read_consensus(record("AMD"))
    assert out.full_year_eps is None and out.full_year_revenue is None
    assert out.reason == "no_full_year_consensus"


def test_a_summary_with_no_consensus_sentence_at_all_says_so():
    out = read_consensus(record("ACEL"))
    assert out.full_year_period == ""
    assert out.reason == "no_consensus_clause"


def test_a_consensus_stated_as_a_loss_keeps_its_sign():
    """AIRG's sentence reads "The current consensus estimate is a loss of
    $0.03 per share". Read as +0.03 it turns a company analysts expect to
    lose money into one they expect to earn it, and a guidance above the bar
    would come out as a beat against a bar with the wrong sign."""
    body = payload("AIRG")
    body["summary"] = body["summary"].replace(
        "for the quarter ending June 30, 2026",
        "for the year ending December 31, 2026")
    out = read_consensus(parse_details(body))
    assert out.full_year_eps == pytest.approx(-0.03)


def test_a_year_the_prose_and_the_feeds_own_reference_disagree_on_is_refused():
    # Two independent statements of one fact. When they differ neither is
    # used — a yardstick from the wrong year is worse than no yardstick.
    body = payload("ADM")
    body["fY1Ref"] = "202712"
    out = read_consensus(parse_details(body))
    assert out.full_year_eps is None
    assert out.reason == "full_year_period_disputed"


# -- the quarterly yardstick in the same prose -----------------------------
#
# After a print the consensus sentence describes the quarter AHEAD, which is
# the quarter the guidance given at that print is about. Measured over the
# 47-name corpus 2026-08-09: 12 records state one, and in all 12 it is the
# quarter immediately after the one reported.

def test_a_next_quarter_consensus_carries_the_quarter_it_is_for():
    # AMD reported the quarter ending June 2026; the sentence names September.
    out = read_consensus(record("AMD"))
    assert out.next_quarter_revenue == pytest.approx(12_620_000_000.0)
    assert out.next_quarter_period == "2026-Q3"


def test_a_non_calendar_year_end_still_names_the_quarter_that_follows():
    # NVDA's quarters end in April/July; the sentence names July after an
    # April print, and the label has to follow the company's own year end.
    out = read_consensus(record("NVDA"))
    assert out.next_quarter_period == "2027-Q2"


def test_a_quarter_that_is_not_the_one_after_this_print_is_refused():
    """The sentence names December while the print covers June — two quarters
    apart. Used as "next quarter" it would put a bar from six months later
    under this print's guidance, which is the cross-period error that read as
    a +348% guidance beat one period up (ADM)."""
    body = payload("AMD")
    body["summary"] = body["summary"].replace(
        "for the quarter ending September 30, 2026",
        "for the quarter ending December 31, 2026")
    out = read_consensus(parse_details(body))
    assert out.next_quarter_revenue is None
    assert out.next_quarter_period == ""
    assert out.next_quarter_reason == "next_quarter_period_disputed"


def test_a_summary_with_no_quarterly_sentence_names_its_absence():
    out = read_consensus(record("ACA"))
    assert out.next_quarter_eps is None
    assert out.next_quarter_reason == "no_next_quarter_consensus"


# -- the whole recorded corpus ---------------------------------------------
#
# The standard the user set: for EVERY name, each leg is either a value or a
# reason somebody can read. A silent None is the failure this pins down.

ALL = sorted(p.stem for p in FIXTURES.glob("*.json"))

_LEGS = (("eps_actual", "eps_actual_missing"),
         ("eps_consensus", "eps_consensus_missing"),
         ("revenue_actual", "revenue_actual_missing"),
         ("revenue_consensus", "revenue_consensus_missing"))

# Every reason this system is allowed to give. A new one has to be added here
# deliberately, which is what stops "we could not read it" from quietly
# growing new spellings nobody reviews.
_KNOWN_REASONS = {"", "no_guidance_clause", "full_year_only",
                  "non_numeric_range", "open_ended_range", "quarter_mismatch",
                  "period_unstated", "quarter_reference_missing",
                  "range_inverted"}


def test_the_corpus_is_large_enough_to_mean_something():
    assert len(ALL) >= 40


@pytest.mark.parametrize("ticker", ALL)
def test_every_number_is_either_a_value_or_a_named_gap(ticker):
    row = record(ticker)
    for field, gap in _LEGS:
        assert getattr(row, field) is not None or gap in row.gaps, (
            f"{ticker}: {field} is missing and nothing says why")


@pytest.mark.parametrize("ticker", ALL)
def test_every_guidance_outcome_carries_a_reason_from_the_known_set(ticker):
    out = read_guidance(record(ticker))
    for reason in out.reason.split(";"):
        assert reason in _KNOWN_REASONS, (
            f"{ticker}: unexplained guidance outcome {out.reason!r} — "
            f"clause was {out.excerpt!r}")
    assert (out.reading is not None or out.full_year is not None
            or out.reason), f"{ticker}: no guidance and no reason"


_KNOWN_CONSENSUS_REASONS = {"", "no_consensus_clause", "no_full_year_consensus",
                            "full_year_period_disputed",
                            "full_year_amount_unreadable"}


@pytest.mark.parametrize("ticker", ALL)
def test_every_consensus_outcome_carries_a_reason_from_the_known_set(ticker):
    out = read_consensus(record(ticker))
    assert out.reason in _KNOWN_CONSENSUS_REASONS, (
        f"{ticker}: unexplained consensus outcome {out.reason!r} — "
        f"clause was {out.excerpt!r}")
    assert (out.full_year_eps is not None or out.full_year_revenue is not None
            or out.reason), f"{ticker}: no full-year yardstick and no reason"


def test_the_full_year_yardstick_is_read_for_every_name_that_states_one():
    # 18 of the 47 recorded summaries carry "for the year ending" (measured
    # 2026-08-09). A parser that reads fewer is dropping yardsticks that are
    # sitting in a string the scan already holds.
    stated = [t for t in ALL
              if "for the year ending" in (record(t).summary or "")]
    read = [t for t in stated
            if read_consensus(record(t)).full_year_eps is not None
            or read_consensus(record(t)).full_year_revenue is not None]
    assert len(stated) == 18
    assert sorted(read) == sorted(stated), (
        f"stated but not read: {sorted(set(stated) - set(read))}")


def test_no_recorded_response_defeats_the_guidance_parser():
    # "unparsed_clause" means the company DID guide and this parser could not
    # read the form. It is the one outcome that must never appear silently.
    defeated = {t: read_guidance(record(t)).excerpt for t in ALL
                if "unparsed_clause" in read_guidance(record(t)).reason}
    assert not defeated, f"guidance forms the parser cannot read: {defeated}"


# -- transport -------------------------------------------------------------

def test_a_response_that_is_not_an_object_is_a_failure_not_an_empty_result():
    with pytest.raises(WhispersUnavailable):
        parse_details(["not", "an", "object"])


def _source(handler, **kw) -> WhispersSource:
    """A live-shaped reader with the retry pauses removed, so a retry test
    measures the rule and not the clock."""
    kw.setdefault("sleep", lambda seconds: None)
    return WhispersSource(transport=httpx.MockTransport(handler), **kw)


def test_a_successful_response_becomes_a_record():
    source = _source(lambda request: httpx.Response(200, json=payload("ADM")))
    assert source.details("ADM").eps_actual == pytest.approx(1.84)


def test_no_content_means_this_company_has_no_record_and_is_not_an_error():
    # 204 is how the feed says "nothing here" for an unknown ticker.
    source = _source(lambda request: httpx.Response(204))
    assert source.details("ZZZZQQ") is None


def test_a_server_error_is_raised_rather_than_read_as_no_record():
    source = _source(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(WhispersUnavailable):
        source.details("ADM")


def test_a_server_error_that_clears_on_the_retry_still_yields_the_record():
    """The retry exists so that a 500 which IS momentary does not cost the
    print its reading. Whether a given ticker's 500 is momentary is what the
    retry finds out."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="<!DOCTYPE html>")
        return httpx.Response(200, json=payload("ADM"))

    assert _source(handler).details("ADM").eps_actual == pytest.approx(1.84)
    assert calls["n"] == 2


def test_a_connection_failure_is_retried_too():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("reset")
        return httpx.Response(200, json=payload("ADM"))

    assert _source(handler).details("ADM") is not None
    assert calls["n"] == 3          # two retries, unlike a server error


def test_a_persistent_server_error_is_confirmed_once_and_not_held_for():
    """Measured 2026-08-08: a 500 from this endpoint is a property of the
    TICKER, not of the moment — INOD, UMAC and GAIN reproduce the same error
    page on every attempt while other names answer JSON in the same loop. One
    retry establishes that; more would buy the same refusal. And it is raised
    as NON-transient, so the funnel falls back to the calendar instead of
    holding the print for 48 hours over an answer that will not change."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, text="<!DOCTYPE html>")

    with pytest.raises(WhispersUnavailable) as caught:
        _source(handler).details("INOD")
    assert calls["n"] == 2                     # the attempt plus one retry
    assert caught.value.transient is False


def test_a_connection_failure_is_raised_as_transient():
    """Nothing came back at all, so nothing was learned about the company —
    which is exactly the case worth waiting on."""
    def handler(request):
        raise httpx.ConnectError("reset")

    with pytest.raises(WhispersUnavailable) as caught:
        _source(handler).details("ADM")
    assert caught.value.transient is True


def test_a_client_error_is_not_retried():
    """A 4xx is the site answering about THIS request. Repeating it changes
    nothing and is not polite."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(403, text="no")

    with pytest.raises(WhispersUnavailable):
        _source(handler).details("ADM")
    assert calls["n"] == 1


def test_an_empty_answer_is_not_retried_because_it_is_a_real_answer():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(204)

    assert _source(handler).details("ZZZZQQ") is None
    assert calls["n"] == 1


def test_a_body_that_is_not_json_is_raised_rather_than_read_as_no_record():
    # The site answers HTML for a mistyped path; a silent None there would
    # look exactly like a company that reported nothing.
    source = _source(lambda request: httpx.Response(
        200, text="<!DOCTYPE html>", headers={"content-type": "text/html"}))
    with pytest.raises(WhispersUnavailable):
        source.details("ADM")


def test_the_request_identifies_itself_the_way_the_feed_requires():
    seen: dict = {}

    def handler(request):
        seen.update(request.headers)
        seen["url"] = str(request.url)
        return httpx.Response(200, json=payload("ADM"))

    _source(handler).details("adm")
    assert seen["url"].endswith("/api/epsdetails/ADM")
    assert "Mozilla" in seen["user-agent"]
    assert seen["referer"].endswith("/epsdetails/ADM")
