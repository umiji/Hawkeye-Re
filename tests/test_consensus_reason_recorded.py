"""Why a full-year yardstick was refused, all the way to the scan's record.

`read_consensus` names every refusal — a year the response contradicts itself
about, an amount it states unreadably — and until T-021 every one of those
names was dropped on the floor one line after it was produced
(`hawkeye/scout/numbers.py`). The figures went missing and nothing anywhere
said why: the scan report told the user "no full-year consensus available",
the tribunal argued a company's outlook as unverified, and the 172 consensus
rows already in the ledger carry no reason at all, so the JKHY defect could
not be counted backwards even once it was understood.

The name has to survive four hops to be worth anything — feed reading ->
event -> candidate -> the scan's own record — because the question it answers
("how often does the safety catch fire, and on which names?") is asked months
later, off the ledger, not during the run.

`no_full_year_consensus` and its siblings are carried too but are NOT
refusals: they mean the response never stated a yardstick. Only the refusals
reach the user's column, or the column would repeat "the feed stated none"
on nearly every row (29 of the 47-name corpus, measured 2026-08-09).
"""
from __future__ import annotations

from datetime import date, timedelta

from tests.conftest import FakeWhispers, make_bars, make_whispers

from hawkeye.config import HawkeyeConfig
from hawkeye.ledger.stocks import StockStore
from hawkeye.marketdata.base import StaticProvider
from hawkeye.scout.earnings import EarningsEvent, screen_events
from hawkeye.scout.numbers import read_numbers
from hawkeye.scout.scan_store import _candidate_from_dict, _candidate_to_dict
from hawkeye.scout.scout import build_screened_candidates, run_scout

DAY = date(2026, 7, 31)

# One sentence naming a year the record's own `fY1Ref` contradicts by two
# steps — the refusal T-021 deliberately did NOT widen away.
DISPUTED = ("The current consensus earnings estimate is $4.10 per share on "
            "revenue of $9.00 billion for the year ending December 31, 2028.")
AGREED = ("The current consensus earnings estimate is $4.10 per share on "
          "revenue of $9.00 billion for the year ending December 31, 2026.")


def _event(ticker="AMZN", **kw) -> EarningsEvent:
    base = dict(ticker=ticker, day=DAY, eps_actual=1.20, eps_estimate=1.00,
                revenue_actual=1.05e9, revenue_estimate=1.0e9,
                fiscal_quarter="2026-Q2")
    base.update(kw)
    return EarningsEvent(**base)


def _read(summary: str) -> EarningsEvent:
    event = _event()
    feed = FakeWhispers({"AMZN": make_whispers("AMZN", announced=DAY,
                                               summary=summary)})
    out, _ = read_numbers([event], screen_events([event], 5.0, 0.0, 0.10, 50.0),
                          feed, limit=5)
    return out[0]


# --- hop 1: the reading no longer drops it --------------------------------

def test_a_refused_full_year_yardstick_leaves_its_reason_on_the_event():
    out = _read(DISPUTED)
    assert out.full_year_eps_estimate is None
    assert out.full_year_consensus_reason == "full_year_period_disputed"


def test_an_accepted_full_year_yardstick_leaves_no_reason():
    out = _read(AGREED)
    assert out.full_year_eps_estimate == 4.10
    assert out.full_year_consensus_reason == ""


def test_the_quarterly_yardsticks_reason_is_carried_separately():
    """Separate fields, because one shared one would report the year's
    refusal as the quarter's — the split `ConsensusReadout` already makes."""
    out = _read(DISPUTED)
    assert out.next_quarter_consensus_reason == "no_next_quarter_consensus"


def test_a_print_the_feed_was_never_asked_about_carries_no_reason():
    event = _event()
    out, _ = read_numbers([event], screen_events([event], 5.0, 0.0, 0.10, 50.0),
                          source=None, limit=5)
    assert out[0].full_year_consensus_reason == ""


# --- hops 2-4: event -> candidate -> the scan's own record ----------------

def _scan(tmp_path, summary: str):
    today = date.today()
    event_day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    entries = [{"symbol": "AMZN", "date": event_day.isoformat(),
                "year": 2026, "quarter": 2,
                "epsActual": 1.20, "epsEstimate": 1.00,
                "revenueActual": 1.05e9, "revenueEstimate": 1.0e9}]

    class _Calendar:
        def earnings_calendar(self, start, end):
            return entries

    feed = FakeWhispers({"AMZN": make_whispers("AMZN", announced=event_day,
                                               summary=summary)})
    result = run_scout(
        _Calendar(),
        StaticProvider(bars=make_bars(30, start_price=40.0,
                                      volume=2_000_000),
                       profile_data={"market_cap": 5e9}),
        HawkeyeConfig(), today=today, stock_store=store, numbers_source=feed)
    return result, build_screened_candidates(result, scan_id=1)


def test_the_refusal_reaches_the_scans_own_record(tmp_path):
    """T-021 completion criterion 4. `build_screened_candidates` writes what
    the run did not forward, and with `sent_to_tribunal_n` left at 0 that is
    every name that passed — which is where JKHY sat on scan 4."""
    result, candidates = _scan(tmp_path, DISPUTED)

    assert [c.ticker for c in result.passed] == ["AMZN"]
    assert result.passed[0].full_year_consensus_reason \
        == "full_year_period_disputed"
    assert [c.full_year_consensus_reason for c in candidates] \
        == ["full_year_period_disputed"]


def test_an_accepted_yardstick_records_no_refusal(tmp_path):
    _result, candidates = _scan(tmp_path, AGREED)
    assert [c.full_year_consensus_reason for c in candidates] == [""]


def test_the_refusal_survives_the_file_the_scan_hands_to_the_ranking(tmp_path):
    """`hawkeye scout` writes a pending file that `hawkeye rank` reads back
    hours later. A field missing from that round trip reaches the ledger
    empty, which reads as "nothing was refused"."""
    result, _ = _scan(tmp_path, DISPUTED)
    restored = _candidate_from_dict(_candidate_to_dict(result.passed[0]))
    assert restored.full_year_consensus_reason == "full_year_period_disputed"


def test_a_pending_file_written_before_this_field_existed_still_loads(tmp_path):
    """Invariant 1: an older record loads unchanged rather than raising."""
    result, _ = _scan(tmp_path, DISPUTED)
    older = _candidate_to_dict(result.passed[0])
    older.pop("full_year_consensus_reason")
    older.pop("next_quarter_consensus_reason")
    assert _candidate_from_dict(older).full_year_consensus_reason == ""


# --- the user's own column ------------------------------------------------
#
# T-021 completion criterion 5's file-side half. The refusal goes into the
# column that already exists (「取得できなかった理由」) rather than a new one:
# T-018 is still settling the CSV's column list, and a seventh guidance column
# would move its completion criterion 1 under it (User decision 2026-08-19).

import csv                                                          # noqa: E402
import io                                                           # noqa: E402

from hawkeye.contracts.models import (                              # noqa: E402
    ScreenedCandidate,
    ScreenedCandidateStage,
)
from hawkeye.reports.scan_report_ja import scan_report_csv           # noqa: E402


def _cells(**overrides) -> dict:
    base = dict(scan_id=4, ticker="JKHY", event_date=date(2026, 8, 18),
                eps_surprise_pct=3.4, revenue_surprise_pct=0.2,
                stage=ScreenedCandidateStage.RANKING_CUTOFF, rank=2,
                score=28.66, score_version="full")
    base.update(overrides)
    header, row = list(csv.reader(io.StringIO(
        scan_report_csv([ScreenedCandidate(**base)]))))[:2]
    return dict(zip(header, row))


def test_the_refusal_is_written_into_the_reason_column_the_file_already_has():
    cell = _cells(full_year_consensus_reason="full_year_period_disputed")[
        "取得できなかった理由"]
    assert "通期" in cell and "年度" in cell
    assert cell != ""


def test_a_refusal_does_not_displace_the_reason_the_column_already_carried():
    """The surprise figures and the yardstick fail independently, and one
    response can refuse both. Overwriting would trade one fact for another."""
    cell = _cells(numbers_reason="whispers_eps_incomplete",
                  full_year_consensus_reason="full_year_period_disputed")[
        "取得できなかった理由"]
    assert "EPS" in cell            # the numbers side survived
    assert "通期" in cell           # and the yardstick side arrived


def test_a_response_that_simply_stated_no_yardstick_is_not_reported_as_one():
    """`no_full_year_consensus` is not a refusal — the sentence named no year
    at all, which 29 of the 47-name corpus do. Printing it would fill the
    column on nearly every row and bury the refusals it exists to show."""
    assert _cells(full_year_consensus_reason="no_full_year_consensus")[
        "取得できなかった理由"] == ""
    assert _cells(next_quarter_consensus_reason="no_next_quarter_consensus")[
        "取得できなかった理由"] == ""


def test_no_column_was_added_to_the_file():
    """T-018 is still settling this header row; T-021 must not move it."""
    header = list(csv.reader(io.StringIO(scan_report_csv([]))))[0]
    assert len(header) == 31
    assert "取得できなかった理由" in header
