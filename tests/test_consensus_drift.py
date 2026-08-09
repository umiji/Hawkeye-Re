"""Measuring whether pre-registration still buys anything (2026-08-09).

Pre-registration exists because of a claim written when the numbers came from
Yahoo: after a release the consensus that release was judged against is gone
for ever, so it has to be captured beforehand. Yahoo's period labels are
relative to today, so that claim was true there.

It may not be true of the earnings feed. `/api/epsdetails/` states the
reported quarter's OWN consensus (`estimate`) after the print — ADM 1.42
against an actual of 1.84, read on 2026-08-09 — which is exactly the figure
pre-registration was invented to preserve.

So the question is now empirical and narrow: does the number captured before
the print equal the number the feed reports after it? If it does,
pre-registration costs ~600 requests a run and changes no judgment. If it
does not, the gap is the most important thing this system has measured about
its own inputs. This module answers that and nothing else — it never writes
to the ledger and never changes a verdict.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from hawkeye.contracts.stocks import ConsensusSnapshot, SnapshotKind, Stock
from hawkeye.ledger.stocks import StockStore
from hawkeye.marketdata.whispers import WhispersUnavailable
from hawkeye.scout.drift import (
    DriftStatus,
    measure_consensus_drift,
    report_line,
)
from tests.conftest import FakeWhispers, make_whispers

JST = timezone(timedelta(hours=9))
TODAY = date(2026, 8, 12)
REPORTED_ON = date(2026, 8, 10)


def store_with(tmp_path, **snapshot) -> tuple[StockStore, str]:
    store = StockStore(str(tmp_path / "hawkeye.db"))
    stock_id = store.put_stock(Stock(ticker=snapshot.get("ticker", "AAA")))
    base = dict(
        stock_id=stock_id, ticker="AAA", fiscal_quarter="2026-Q2",
        captured_at=datetime(2026, 8, 9, 9, tzinfo=JST),
        kind=SnapshotKind.PRE_REGISTERED, expected_report_date=REPORTED_ON,
        eps_avg=1.00, revenue_avg=1.00e9, eps_calendar=1.00)
    base.update(snapshot)
    store.capture_consensus(ConsensusSnapshot(**base))
    return store, stock_id


def feed(**overrides) -> FakeWhispers:
    return FakeWhispers({"AAA": make_whispers(
        "AAA", announced=REPORTED_ON, **overrides)})


# -- the measurement -------------------------------------------------------

def test_a_consensus_that_did_not_move_is_reported_as_unchanged(tmp_path):
    store, _ = store_with(tmp_path)
    out = measure_consensus_drift(
        store, feed(eps_consensus=1.00, revenue_consensus=1.00e9), today=TODAY)

    assert [r.status for r in out.readings] == [DriftStatus.UNCHANGED]
    assert out.unchanged == 1 and out.moved == 0


def test_a_consensus_that_moved_reports_the_gap_in_both_directions(tmp_path):
    store, _ = store_with(tmp_path)
    out = measure_consensus_drift(
        store, feed(eps_consensus=1.10, revenue_consensus=0.95e9), today=TODAY)

    reading = out.readings[0]
    assert reading.status is DriftStatus.MOVED
    assert reading.eps_before == 1.00 and reading.eps_after == 1.10
    assert reading.eps_drift_pct == 10.0
    assert reading.revenue_drift_pct == -5.0


def test_a_move_below_the_published_precision_is_not_a_move(tmp_path):
    """EPS is published to the cent, so a difference under half a cent is
    rounding in the feed, not analysts changing their minds. Counting it as a
    move would make the measurement say "the number always moves" — which is
    the answer that would keep pre-registration for no reason."""
    store, _ = store_with(tmp_path)
    out = measure_consensus_drift(
        store, feed(eps_consensus=1.0004, revenue_consensus=1.0000004e9),
        today=TODAY)

    assert out.readings[0].status is DriftStatus.UNCHANGED


def test_the_days_between_the_two_readings_are_recorded(tmp_path):
    """A gap of one day and a gap of ten are different measurements, and a
    single "they differed" number that pools them says nothing."""
    store, _ = store_with(tmp_path)
    out = measure_consensus_drift(store, feed(), today=TODAY)

    assert out.readings[0].days_apart == 1     # captured 08-09, printed 08-10


# -- what it refuses to compare --------------------------------------------

def test_a_print_the_feed_has_not_ingested_yet_is_not_a_comparison(tmp_path):
    """The date has arrived but the feed still answers with the PREVIOUS
    quarter. Comparing against that would measure the gap between two
    different quarters and report it as estimate drift. Worth re-checking
    tomorrow, so it is counted as "not reported yet" rather than refused."""
    store, _ = store_with(tmp_path, fiscal_quarter="2026-Q3")
    out = measure_consensus_drift(store, feed(fiscal_quarter="2026-Q2"),
                                  today=TODAY)

    assert [r.status for r in out.readings] == [DriftStatus.NOT_YET_REPORTED]
    assert out.unchanged == 0 and out.moved == 0


def test_a_feed_row_for_a_LATER_quarter_will_never_be_comparable(tmp_path):
    """The feed keeps only a company's latest print, so once the NEXT quarter
    has reported the pre-registered one can never be checked. That is a
    permanent loss and is counted apart from "come back tomorrow" — it is
    also the reason to run this measurement early rather than late."""
    store, _ = store_with(tmp_path)
    out = measure_consensus_drift(store, feed(fiscal_quarter="2026-Q3"),
                                  today=TODAY)

    assert out.readings[0].status is DriftStatus.QUARTER_MISMATCH


def test_a_row_with_no_pre_print_figure_is_nothing_to_compare(tmp_path):
    """Pre-registration writes a row whenever the calendar alone had a
    number, so plenty of rows carry no reading from the feed at all."""
    store, _ = store_with(tmp_path, eps_avg=None, revenue_avg=None)
    out = measure_consensus_drift(store, feed(), today=TODAY)

    assert out.readings[0].status is DriftStatus.NOTHING_TO_COMPARE


def test_a_feed_that_cannot_be_read_is_counted_apart(tmp_path):
    store, _ = store_with(tmp_path)
    source = FakeWhispers({"AAA": WhispersUnavailable("connection reset")})
    out = measure_consensus_drift(store, source, today=TODAY)

    assert out.readings[0].status is DriftStatus.UNREADABLE
    assert out.unreadable == 1


def test_a_reconstructed_row_is_never_measured(tmp_path):
    """It was written AFTER the print, from the same response this would
    compare it against. Including it would report a 100% match rate that
    means nothing."""
    store, _ = store_with(tmp_path, kind=SnapshotKind.RECONSTRUCTED)
    out = measure_consensus_drift(store, feed(), today=TODAY)

    assert out.readings == []


def test_nothing_is_written_to_the_ledger(tmp_path):
    """A measurement, not a capture. Writing a row here would put a figure
    read after the print into the pre-registration record."""
    store, stock_id = store_with(tmp_path)
    before = len(store.consensus_snapshots(stock_id, "2026-Q2"))

    measure_consensus_drift(store, feed(eps_consensus=1.10), today=TODAY)

    assert len(store.consensus_snapshots(stock_id, "2026-Q2")) == before


# -- the request budget ----------------------------------------------------

def test_only_names_whose_print_is_due_cost_a_request(tmp_path):
    """A row for a print still days away cannot have moved yet, and asking
    about it spends a request to learn nothing."""
    store, _ = store_with(tmp_path, expected_report_date=TODAY + timedelta(
        days=5))
    source = feed()

    measure_consensus_drift(store, source, today=TODAY)

    assert source.asked == []


def test_the_limit_caps_the_requests(tmp_path):
    store = StockStore(str(tmp_path / "hawkeye.db"))
    for ticker in ("AAA", "BBB", "CCC"):
        stock_id = store.put_stock(Stock(ticker=ticker))
        store.capture_consensus(ConsensusSnapshot(
            stock_id=stock_id, ticker=ticker, fiscal_quarter="2026-Q2",
            captured_at=datetime(2026, 8, 9, 9, tzinfo=JST),
            kind=SnapshotKind.PRE_REGISTERED,
            expected_report_date=REPORTED_ON, eps_avg=1.00))
    source = FakeWhispers({t: make_whispers(t, announced=REPORTED_ON)
                           for t in ("AAA", "BBB", "CCC")})

    measure_consensus_drift(store, source, today=TODAY, limit=2)

    assert len(source.asked) == 2


# -- the report ------------------------------------------------------------

def test_the_report_says_what_the_measurement_is_for(tmp_path):
    store, _ = store_with(tmp_path)
    out = measure_consensus_drift(store, feed(eps_consensus=1.10),
                                  today=TODAY)

    line = report_line(out)
    assert "動いた 1 件" in line
    assert "変わらなかった 0 件" in line


def test_an_empty_run_says_so_rather_than_reporting_a_perfect_match(tmp_path):
    """Zero comparisons and zero disagreements read the same in a bare
    percentage. A run with nothing to measure has to say that outright."""
    store = StockStore(str(tmp_path / "hawkeye.db"))
    out = measure_consensus_drift(store, feed(), today=TODAY)

    assert out.compared == 0
    assert "比較できた組が0件" in report_line(out)
