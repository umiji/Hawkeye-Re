"""Choosing whose figures a print stands on, before the ranking.

Fully offline: the feed is a stub returning `WhispersRecord` objects, so what
these tests pin is our selection rule, not the site's availability on the day
the suite runs.

The rule under test is one sentence: **one print, one vendor, both legs.** A
surprise ratio whose numerator and denominator come from different vendors is
arithmetic without meaning — the feed's consensus is an adjusted-basis figure
and the calendar's actual may be GAAP — so the choice is made once, for the
whole print, and recorded.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from hawkeye.marketdata.whispers import (
    EASTERN,
    WhispersRecord,
    WhispersUnavailable,
)
from hawkeye.scout.earnings import (
    EarningsEvent,
    eps_surprise_pct,
    screen_events,
)
from hawkeye.scout.numbers import numbers_targets, read_numbers

DAY = date(2026, 7, 30)


def _record(ticker="AAA", announced=DAY, eps_actual=1.30, eps_consensus=1.00,
            revenue_actual=1.05e9, revenue_consensus=1.00e9,
            **kw) -> WhispersRecord:
    base = dict(
        ticker=ticker, name=ticker, quarter_end=date(2026, 6, 30),
        fiscal_quarter="2026-Q2",
        announced_at=(datetime(announced.year, announced.month, announced.day,
                               16, 5, tzinfo=EASTERN)
                      if announced is not None else None),
        eps_actual=eps_actual, eps_consensus=eps_consensus,
        eps_consensus_high=None, eps_consensus_low=None,
        revenue_actual=revenue_actual, revenue_consensus=revenue_consensus,
        whisper=None)
    base.update(kw)
    return WhispersRecord(**base)


def _event(ticker="AAA", day=DAY, actual=1.20, estimate=1.00,
           **kw) -> EarningsEvent:
    return EarningsEvent(ticker=ticker, day=day, eps_actual=actual,
                         eps_estimate=estimate, revenue_actual=1.02e9,
                         revenue_estimate=1.00e9, **kw)


class _Feed:
    """The earnings feed, stubbed. A value of `WhispersUnavailable` is raised
    rather than returned, exactly as the live reader does."""

    def __init__(self, by_ticker: dict):
        self.by_ticker = by_ticker
        self.asked: list[str] = []

    def details(self, ticker: str):
        self.asked.append(ticker)
        answer = self.by_ticker.get(ticker)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _screened(events):
    return screen_events(events, 5.0, 0.0, 0.10, 1e9)


# --- who gets asked --------------------------------------------------------

def test_screen_survivors_come_first_and_the_budget_caps_the_rest():
    kept = _event("AAA", actual=1.20, estimate=1.00)
    suspect = _event("ZZZ", actual=1.0, estimate=1.0,
                     conflicting_estimates=True)
    assert numbers_targets([kept, suspect], _screened([kept]), limit=1) == [
        ("AAA", DAY)]


def test_a_name_the_screen_dropped_on_conflicting_rows_is_still_read():
    """The asymmetric failure mode kept from the Yahoo pass: collapsing
    BJRI's contradictory calendar rows to the conservative reading pushed its
    real +3.5% below the 5% screen, so the CORRECT reading was the one that
    vanished. A false negative leaves no trace, so this tier is the only
    thing that can catch it."""
    suspect = _event("ZZZ", actual=1.0, estimate=1.0,
                     conflicting_estimates=True)
    assert numbers_targets([suspect], screened=[], limit=10) == [("ZZZ", DAY)]


def test_a_print_an_earlier_scan_recorded_is_never_read_again():
    kept = _event("AAA")
    suspect = _event("ZZZ", actual=1.0, estimate=1.0,
                     conflicting_estimates=True)
    assert numbers_targets([kept, suspect], _screened([kept]), limit=10,
                           skip={("AAA", DAY)}) == [("ZZZ", DAY)]


def test_a_named_print_is_read_even_when_an_earlier_scan_saw_it():
    event = _event("AAA")
    assert numbers_targets([event], screened=[], limit=10,
                           always=[("AAA", DAY)],
                           skip={("AAA", DAY)}) == [("AAA", DAY)]


# --- the feed supplies both legs, or neither ------------------------------

def test_the_feed_replaces_every_number_not_just_eps():
    """Yahoo could only ever supply EPS, so revenue stayed the calendar's and
    the revenue ratio mixed vendors. The feed answers both legs in one
    request, so both move together or neither does."""
    event = _event()
    feed = _Feed({"AAA": _record(eps_actual=1.31, eps_consensus=1.01,
                                 revenue_actual=1.10e9,
                                 revenue_consensus=1.02e9)})
    out, stats = read_numbers([event], _screened([event]), feed, limit=5)

    assert out[0].numbers_source == "whispers"
    assert out[0].numbers_reason == ""
    assert (out[0].eps_actual, out[0].eps_estimate) == (1.31, 1.01)
    assert (out[0].revenue_actual, out[0].revenue_estimate) == (1.10e9, 1.02e9)
    assert stats.from_whispers == 1


def test_the_calendars_own_reading_is_kept_beside_the_feeds():
    """Both readings are needed: how far the vendors disagree is itself data,
    and it cannot be measured against a value that was overwritten."""
    event = _event(actual=1.20, estimate=1.00)
    feed = _Feed({"AAA": _record(eps_actual=1.31, eps_consensus=1.01,
                                 revenue_actual=1.10e9,
                                 revenue_consensus=1.02e9)})
    out, _ = read_numbers([event], _screened([event]), feed, limit=5)

    assert (out[0].calendar_eps_actual, out[0].calendar_eps_estimate) == (
        1.20, 1.00)
    assert (out[0].calendar_revenue_actual,
            out[0].calendar_revenue_estimate) == (1.02e9, 1.00e9)
    assert out[0].calendar_eps_surprise_pct == pytest.approx(20.0)


def test_a_missing_revenue_consensus_sends_the_whole_print_to_the_calendar():
    """Not just the revenue leg. Keeping the feed's EPS while the revenue
    ratio came from the calendar is exactly the mixing this rule forbids —
    and a missing revenue consensus is the common case, 7 of 47 measured."""
    event = _event()
    feed = _Feed({"AAA": _record(revenue_consensus=None)})
    out, stats = read_numbers([event], _screened([event]), feed, limit=5)

    assert out[0].numbers_source == "calendar"
    assert out[0].numbers_reason == "whispers_revenue_incomplete"
    assert (out[0].eps_actual, out[0].eps_estimate) == (1.20, 1.00)
    assert stats.fell_back == 1 and stats.from_whispers == 0


def test_a_missing_eps_consensus_sends_the_whole_print_to_the_calendar():
    event = _event()
    feed = _Feed({"AAA": _record(eps_consensus=None)})
    out, _ = read_numbers([event], _screened([event]), feed, limit=5)
    assert out[0].numbers_reason == "whispers_eps_incomplete"


def test_the_guidance_survives_a_fallback_on_the_numbers():
    """The one-vendor rule governs the surprise RATIO — its actual and its
    consensus. Guidance is a third leg measured against next quarter's
    consensus, so reading it off the feed's prose while the ratio stands on
    the calendar's numbers mixes nothing. Throwing it away was pure loss: 8 of
    50 names on a live run declined for a missing revenue consensus while the
    same response held their guidance sentence, and guidance has no other free
    source anywhere."""
    event = _event()
    feed = _Feed({"AAA": _record(revenue_consensus=None, summary=(
        "The company said it expects third quarter earnings of $1.10 to "
        "$1.20 per share."))})
    out, _ = read_numbers([event], _screened([event]), feed, limit=5)

    assert out[0].numbers_source == "calendar"          # the ratio fell back
    assert out[0].numbers_reason == "whispers_revenue_incomplete"
    assert out[0].guidance is not None                  # the third leg did not
    assert out[0].guidance.period == "2026-Q3"
    assert out[0].announced_at is not None


def test_guidance_from_a_record_about_a_DIFFERENT_print_is_refused():
    """A stale record's summary describes the PREVIOUS quarter's guidance.
    Attaching it would put last quarter's outlook on this quarter's print."""
    event = _event()
    feed = _Feed({"AAA": _record(announced=date(2026, 4, 28), summary=(
        "The company said it expects third quarter earnings of $1.10 to "
        "$1.20 per share."))})
    out, _ = read_numbers([event], _screened([event]), feed, limit=5)

    assert out[0].numbers_reason == "whispers_previous_quarter"
    assert out[0].guidance is None


# --- the feed answering about a DIFFERENT print ---------------------------

def test_a_record_from_the_previous_quarter_is_refused_and_named():
    """The feed answers with the company's LATEST print, which for about a
    day after a release is still last quarter — same shape, same fields,
    another quarter entirely. Naming it rather than returning a bare failure
    is what lets the 48-hour hold tell "not ingested yet" (worth waiting for)
    from "the calendar's date was wrong" (waiting will not help)."""
    event = _event()
    feed = _Feed({"AAA": _record(announced=date(2026, 4, 28))})
    out, stats = read_numbers([event], _screened([event]), feed, limit=5)

    assert out[0].numbers_source == "calendar"
    assert out[0].numbers_reason == "whispers_previous_quarter"
    assert stats.stale == 1


def test_a_record_from_a_later_print_is_refused_under_its_own_name():
    event = _event()
    feed = _Feed({"AAA": _record(announced=date(2026, 10, 29))})
    out, _ = read_numbers([event], _screened([event]), feed, limit=5)
    assert out[0].numbers_reason == "whispers_later_print"


def test_a_one_day_gap_is_the_same_print():
    """Vendors disagree by a day on prints released after the close."""
    event = _event()
    feed = _Feed({"AAA": _record(announced=date(2026, 7, 31))})
    out, _ = read_numbers([event], _screened([event]), feed, limit=5)
    assert out[0].numbers_source == "whispers"


# --- the connection failing is not a fact about the company ---------------

def test_an_unreachable_feed_reads_as_unreachable_not_as_absent():
    event = _event()
    feed = _Feed({"AAA": WhispersUnavailable("boom")})
    out, stats = read_numbers([event], _screened([event]), feed, limit=5)

    assert out[0].numbers_reason == "whispers_unreachable"
    assert stats.unreachable == 1
    assert stats.fell_back == 0        # counted apart, deliberately


def test_a_company_the_feed_has_no_record_for_says_so():
    event = _event()
    feed = _Feed({"AAA": None})
    out, _ = read_numbers([event], _screened([event]), feed, limit=5)
    assert out[0].numbers_reason == "whispers_no_record"


# --- the vendor's own surprise figure -------------------------------------

def test_the_vendors_surprise_is_taken_only_when_it_is_against_consensus():
    """Where a whisper number exists the feed measures against IT, not
    against consensus: AMD 2026-08-04 reads -1.78% from the vendor and
    +2.47% from actual against consensus. Copying the vendor's figure
    blindly would put "missed expectations" on a print that beat."""
    event = _event()
    feed = _Feed({"AAA": _record(eps_actual=1.30, eps_consensus=1.00,
                                 vendor_eps_surprise_pct=-1.78,
                                 vendor_surprise_basis="whisper")})
    out, _ = read_numbers([event], _screened([event]), feed, limit=5)

    assert out[0].eps_surprise_pct_reported is None
    assert eps_surprise_pct(out[0]) == pytest.approx(30.0)


def test_a_consensus_based_vendor_surprise_wins_over_recomputing():
    event = _event()
    feed = _Feed({"AAA": _record(eps_actual=0.94, eps_consensus=0.90,
                                 vendor_eps_surprise_pct=4.95,
                                 vendor_surprise_basis="consensus")})
    out, _ = read_numbers([event], _screened([event]), feed, limit=5)
    assert eps_surprise_pct(out[0]) == pytest.approx(4.95)


# --- what travels with the numbers ----------------------------------------

def test_the_feeds_fiscal_label_and_announcement_time_travel_with_it():
    event = _event()
    feed = _Feed({"AAA": _record(fiscal_quarter="2027-Q1")})
    out, _ = read_numbers([event], _screened([event]), feed, limit=5)

    assert out[0].fiscal_quarter == "2027-Q1"
    assert out[0].announced_at is not None


def test_guidance_is_read_off_the_same_response():
    """One request already carries the third leg. Leaving it unread would
    send every print to the tribunal with guidance permanently absent."""
    event = _event()
    feed = _Feed({"AAA": _record(summary=(
        "The company said it expects third quarter earnings of $1.10 to "
        "$1.20 per share. The current consensus is $1.05."))})
    out, _ = read_numbers([event], _screened([event]), feed, limit=5)

    assert out[0].guidance is not None
    assert out[0].guidance.period == "2026-Q3"
    assert out[0].guidance.eps_low == 1.10 and out[0].guidance.eps_high == 1.20


def test_a_company_that_guided_nothing_is_not_confused_with_an_unread_one():
    event = _event()
    feed = _Feed({"AAA": _record(summary="ACME reported second quarter.")})
    out, _ = read_numbers([event], _screened([event]), feed, limit=5)

    assert out[0].guidance is None
    assert out[0].guidance_reason == "no_guidance_clause"


# --- the universe is never narrowed here ----------------------------------

def test_events_outside_the_read_set_are_still_returned_untouched():
    kept = _event("AAA")
    other = _event("ZZZ", actual=0.5, estimate=1.0)
    feed = _Feed({"AAA": _record()})
    out, _ = read_numbers([kept, other], _screened([kept]), feed, limit=10)

    assert [e.ticker for e in out] == ["AAA", "ZZZ"]
    assert out[1] == other
    assert feed.asked == ["AAA"]


def test_without_a_feed_nothing_changes():
    event = _event()
    out, stats = read_numbers([event], [], None, limit=10)
    assert out == [event] and stats.attempted == 0


def test_budget_exhausted_distinguishes_not_asked_from_asked_and_agreed():
    a = _event("AAA", conflicting_estimates=True)
    b = _event("BBB", conflicting_estimates=True)
    feed = _Feed({})
    _, stats = read_numbers([a, b], [], feed, limit=1)
    assert (stats.attempted, stats.budget_exhausted) == (1, True)

    _, stats = read_numbers([a, b], [], _Feed({}), limit=5)
    assert (stats.attempted, stats.budget_exhausted) == (2, False)


def test_each_name_costs_exactly_one_request():
    events = [_event("AAA"), _event("BBB")]
    feed = _Feed({"AAA": _record("AAA"), "BBB": _record("BBB")})
    read_numbers(events, _screened(events), feed, limit=10)
    assert feed.asked == ["AAA", "BBB"]
