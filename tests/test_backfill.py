"""Backfilling the quarters before the one that made a name a candidate.

The point of these tests is what the feed CANNOT give us. Probed live on
2026-08-10: the per-symbol history endpoint returns four rows whatever `limit`
asks for, carries EPS only, and — for AAPL — spends one of those four rows on
a duplicate of another quarter's actual. A backfill that quietly wrote four
clean quarters from that would hand the Adversary a run of beats that the
vendor never actually reported.
"""
from __future__ import annotations

from datetime import date

import pytest

from hawkeye.contracts.stocks import (
    EarningsPrint,
    PrintSource,
    SnapshotKind,
    Stock,
)
from hawkeye.ledger.stocks import StockStore
from hawkeye.scout.backfill import (
    BACKFILL_SKIP_REASONS,
    backfill_history,
    parse_history,
)

# The live response, kept verbatim. AAPL is here BECAUSE it is broken: the
# 2026-Q2 and 2026-Q3 rows both claim an actual of 1.91 against different
# estimates, so one of the two is a stale duplicate and nothing in the payload
# says which.
AAPL_LIVE = [
    {"actual": 1.91, "estimate": 1.9271, "period": "2026-03-31", "quarter": 2,
     "surprise": -0.0171, "surprisePercent": -0.8873, "symbol": "AAPL",
     "year": 2026},
    {"actual": 1.91, "estimate": 2.0512, "period": "2026-06-30", "quarter": 3,
     "surprise": -0.1412, "surprisePercent": -6.8838, "symbol": "AAPL",
     "year": 2026},
    {"actual": 2.01, "estimate": 1.9884, "period": "2025-12-31", "quarter": 1,
     "surprise": 0.0216, "surprisePercent": 1.0863, "symbol": "AAPL",
     "year": 2026},
    {"actual": 1.85, "estimate": 1.8075, "period": "2025-09-30", "quarter": 4,
     "surprise": 0.0425, "surprisePercent": 2.3513, "symbol": "AAPL",
     "year": 2025},
]

MSFT_LIVE = [
    {"actual": 4.74, "estimate": 4.3274, "period": "2026-06-30", "quarter": 4,
     "symbol": "MSFT", "year": 2026},
    {"actual": 4.27, "estimate": 4.1432, "period": "2026-03-31", "quarter": 3,
     "symbol": "MSFT", "year": 2026},
    {"actual": 4.14, "estimate": 4.0345, "period": "2025-12-31", "quarter": 2,
     "symbol": "MSFT", "year": 2026},
    {"actual": 4.13, "estimate": 3.7391, "period": "2025-09-30", "quarter": 1,
     "symbol": "MSFT", "year": 2026},
]


class FakeHistory:
    """The per-symbol history endpoint, stubbed. None means the call did not
    complete, which is a different fact from an empty answer."""

    def __init__(self, by_ticker: dict):
        self.by_ticker = by_ticker
        self.asked: list[str] = []

    def earnings_history(self, ticker: str, limit: int = 4):
        self.asked.append(ticker)
        return self.by_ticker.get(ticker, [])


def _store(tmp_path, ticker: str = "MSFT") -> tuple[StockStore, str]:
    store = StockStore(str(tmp_path / "hawkeye.db"))
    stock = Stock(id="cik:0000789019", ticker=ticker, name=ticker)
    store.put_stock(stock)
    return store, stock.id


# --- parsing ---------------------------------------------------------------

def test_each_row_is_labelled_from_the_feeds_own_year_and_quarter():
    quarters = parse_history(MSFT_LIVE)
    assert [q.fiscal_quarter for q in quarters] == [
        "2026-Q4", "2026-Q3", "2026-Q2", "2026-Q1"]
    assert [q.eps_actual for q in quarters] == [4.74, 4.27, 4.14, 4.13]
    # The estimate is the calendar's single point, not a distribution, and has
    # to stay recognisable as such downstream.
    assert [q.eps_estimate for q in quarters] == [4.3274, 4.1432, 4.0345, 3.7391]
    assert all(q.skip_reason == "" for q in quarters)
    assert all(q.flags == () for q in quarters)


def test_two_quarters_claiming_the_same_actual_are_both_flagged():
    quarters = {q.fiscal_quarter: q for q in parse_history(AAPL_LIVE)}
    assert "repeated_actual" in quarters["2026-Q2"].flags
    assert "repeated_actual" in quarters["2026-Q3"].flags
    # The two quarters that are not part of the duplicate stay clean.
    assert quarters["2026-Q1"].flags == ()
    assert quarters["2025-Q4"].flags == ()


def test_a_row_with_no_year_or_quarter_is_kept_and_says_why():
    quarters = parse_history([{"actual": 1.0, "estimate": 0.9,
                               "period": "2025-09-30", "symbol": "X"}])
    assert quarters[0].skip_reason == "no_fiscal_quarter"


def test_a_row_with_no_period_date_is_kept_and_says_why():
    quarters = parse_history([{"actual": 1.0, "estimate": 0.9, "year": 2026,
                               "quarter": 2, "symbol": "X"}])
    assert quarters[0].skip_reason == "no_period_date"


def test_a_row_with_no_actual_is_kept_and_says_why():
    quarters = parse_history([{"estimate": 0.9, "year": 2026, "quarter": 2,
                               "period": "2026-03-31", "symbol": "X"}])
    assert quarters[0].skip_reason == "no_actual"


# --- writing ---------------------------------------------------------------

def test_one_print_and_one_reconstructed_consensus_per_quarter(tmp_path):
    store, stock_id = _store(tmp_path)
    stats = backfill_history(store, FakeHistory({"MSFT": MSFT_LIVE}),
                             [("MSFT", stock_id)], quarters=4)

    assert stats.quarters_written == 4
    rows = store.active_prints(stock_id)
    assert [r.fiscal_quarter for r in rows] == [
        "2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"]
    assert all(r.source is PrintSource.FINNHUB for r in rows)
    assert all(r.revenue_actual is None for r in rows), (
        "the endpoint carries no revenue; inventing one would be the whole bug")

    # The estimate lands in the field reserved for the calendar's point
    # estimate, never in `eps_avg` — a point and a distribution are not the
    # same evidence, and the surprise screen's denominator depends on which.
    snapshot = store.consensus_in_force(stock_id, "2026-Q4")
    assert snapshot is not None
    assert snapshot.kind is SnapshotKind.RECONSTRUCTED
    assert snapshot.eps_calendar == 4.3274
    assert snapshot.eps_avg is None
    # And the print points at it, so the pairing is not re-derived later.
    written = store.active_print(stock_id, "2026-Q4")
    assert written.consensus_snapshot_id == snapshot.id


def test_a_quarter_already_on_record_is_left_exactly_as_it_was(tmp_path):
    store, stock_id = _store(tmp_path)
    store.record_print(EarningsPrint(
        stock_id=stock_id, ticker="MSFT", fiscal_quarter="2026-Q4",
        report_date=date(2026, 7, 30), source=PrintSource.WHISPERS,
        eps_actual=4.74, revenue_actual=76.4e9))

    stats = backfill_history(store, FakeHistory({"MSFT": MSFT_LIVE}),
                             [("MSFT", stock_id)], quarters=4)

    assert stats.quarters_already_known == 1
    assert stats.quarters_written == 3
    kept = store.active_print(stock_id, "2026-Q4")
    assert kept.source is PrintSource.WHISPERS, "the better row must survive"
    assert kept.revenue_actual == 76.4e9


def test_a_backfilled_row_says_its_date_is_the_period_end(tmp_path):
    store, stock_id = _store(tmp_path)
    backfill_history(store, FakeHistory({"MSFT": MSFT_LIVE}),
                     [("MSFT", stock_id)], quarters=4)
    row = store.active_print(stock_id, "2026-Q4")
    # 2026-06-30 is when the quarter ENDED. Microsoft announced it in late
    # July. The endpoint never says which day it reported, so the row must not
    # let the date read as an announcement date.
    assert row.report_date == date(2026, 6, 30)
    assert "report_date_is_period_end" in row.contamination_flags


def test_the_duplicate_actual_reaches_the_stored_row(tmp_path):
    store, stock_id = _store(tmp_path, "AAPL")
    backfill_history(store, FakeHistory({"AAPL": AAPL_LIVE}),
                     [("AAPL", stock_id)], quarters=4)
    assert "repeated_actual" in store.active_print(
        stock_id, "2026-Q2").contamination_flags
    assert "repeated_actual" not in store.active_print(
        stock_id, "2025-Q4").contamination_flags


def test_a_call_that_never_completed_is_counted_apart_from_an_empty_answer(
        tmp_path):
    store, stock_id = _store(tmp_path)
    silent = FakeHistory({"MSFT": None})
    stats = backfill_history(store, silent, [("MSFT", stock_id)], quarters=4)
    assert stats.tickers_unreachable == 1
    assert stats.quarters_written == 0

    empty = FakeHistory({"MSFT": []})
    stats = backfill_history(store, empty, [("MSFT", stock_id)], quarters=4)
    assert stats.tickers_unreachable == 0
    assert stats.quarters_written == 0


def test_the_quarter_cap_bounds_what_is_asked_for_and_what_is_written(tmp_path):
    store, stock_id = _store(tmp_path)
    feed = FakeHistory({"MSFT": MSFT_LIVE})
    stats = backfill_history(store, feed, [("MSFT", stock_id)], quarters=2)
    assert stats.quarters_written == 2
    # Newest first: a shorter backfill keeps the recent quarters, which are the
    # ones an argument about this print can actually use.
    assert [r.fiscal_quarter for r in store.active_prints(stock_id)] == [
        "2026-Q3", "2026-Q4"]


def test_unusable_rows_are_counted_by_reason_not_lumped_together(tmp_path):
    store, stock_id = _store(tmp_path)
    feed = FakeHistory({"MSFT": [
        {"actual": 1.0, "estimate": 0.9, "period": "2025-09-30"},
        {"estimate": 0.9, "year": 2026, "quarter": 2, "period": "2026-03-31"},
    ]})
    stats = backfill_history(store, feed, [("MSFT", stock_id)], quarters=4)
    assert stats.skipped == {"no_fiscal_quarter": 1, "no_actual": 1}
    assert stats.quarters_written == 0


def test_the_estimate_we_retrieved_is_visible_to_the_reader(tmp_path):
    """A backfilled quarter has a point estimate and no distribution. Rendering
    only the distribution showed 「コンセンサス -」 for a figure we had."""
    from hawkeye.reports.quality_ja import render_stock_history_ja
    store, stock_id = _store(tmp_path)
    backfill_history(store, FakeHistory({"MSFT": MSFT_LIVE}),
                     [("MSFT", stock_id)], quarters=4)
    page = render_stock_history_ja(store.history(stock_id))
    assert "4.3274" in page
    assert "カレンダーの単一予想" in page


@pytest.mark.parametrize("reason", sorted(BACKFILL_SKIP_REASONS))
def test_every_skip_reason_has_a_japanese_gloss(reason):
    """A reason the reader meets as a bare identifier is a reason they cannot
    act on — the same rule the guidance failures are held to."""
    from hawkeye.reports.quality_ja import _FLAG
    assert reason in _FLAG or f"backfill_{reason}" in _FLAG
