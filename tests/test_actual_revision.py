"""Reported figures that change after the fact (task 8.5).

Not the same problem as task 6's hold. There the figures never arrived; here
they arrived and then MOVED. ADEA reported 2026-Q2 on 2026-08-05 and its EPS
actual went from $0.34 to $0.42 the next day — a 24% change, with revenue
untouched — which is enough to flip a beat into a bigger beat, or a beat into
a miss, after the shortlist was already decided on the old number.

Four rules:

1. **A revision is a NEW row.** The old one is retired (`superseded`), not
   overwritten. Overwriting would erase the fact that the shortlist was built
   on $0.34, and a later reader would see a system that had known $0.42 all
   along — quietly biasing both the drop review and the thesis-accuracy score.
2. **Only inside the watch window.** The scan re-reads a print on every
   overlapping window; without a bound, a figure restated months later would
   silently reopen a quarter the ledger had finished with.
3. **The ranking uses the corrected figure**, and the user is shown what
   changed before the ranking table — the point of catching it is to not rank
   on a number the vendor has already withdrawn.
4. **Retired rows can be deleted, active ones never.** The logical retirement
   is the default; the physical delete is a separate, explicit command.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.stocks import (
    EarningsPrint,
    PrintSource,
    RowStatus,
    Stock,
)
from hawkeye.ledger.stocks import StockStore
from hawkeye.scout.revision import detect_revisions, apply_revision

CONFIG = HawkeyeConfig()
REPORTED = date(2026, 8, 5)
ANNOUNCED = datetime(2026, 8, 5, 20, 5, tzinfo=timezone.utc)


def a_print(eps=0.34, revenue=9.5e8, **overrides) -> EarningsPrint:
    base = dict(stock_id="cik:1", ticker="ADEA", fiscal_quarter="2026-Q2",
                report_date=REPORTED, reported_at=ANNOUNCED,
                source=PrintSource.WHISPERS, eps_actual=eps,
                revenue_actual=revenue)
    base.update(overrides)
    return EarningsPrint(**base)


def a_store(tmp_path) -> StockStore:
    store = StockStore(str(tmp_path / "stocks.db"))
    store.put_stock(Stock(id="cik:1", ticker="ADEA", name="Adeia"))
    return store


# --- detection -------------------------------------------------------------

def test_the_same_figures_read_again_are_not_a_revision():
    """Scan windows overlap by design, so a print arrives again on the next
    run. A repeat carrying the same numbers says nothing new."""
    assert detect_revisions(a_print(), a_print(), CONFIG,
                            now=ANNOUNCED + timedelta(hours=6)) == []


def test_a_moved_actual_is_a_revision_naming_both_values():
    revisions = detect_revisions(a_print(eps=0.34), a_print(eps=0.42), CONFIG,
                                 now=ANNOUNCED + timedelta(hours=24))

    assert len(revisions) == 1
    assert revisions[0].field == "eps_actual"
    assert revisions[0].before == 0.34 and revisions[0].after == 0.42
    assert revisions[0].ticker == "ADEA"


def test_a_change_after_the_watch_window_is_not_picked_up():
    """A figure restated months later would reopen a quarter the ledger has
    finished with, on a scan nobody ran for that purpose."""
    late = ANNOUNCED + timedelta(hours=CONFIG.actual_revision_watch_hours + 1)

    assert detect_revisions(a_print(eps=0.34), a_print(eps=0.42), CONFIG,
                            now=late) == []


def test_a_rounding_difference_is_not_a_revision():
    """The vendor publishes EPS to the cent. A difference smaller than what it
    prints is float noise, and reporting it would train the reader to ignore
    the message."""
    assert detect_revisions(a_print(eps=0.34), a_print(eps=0.340001), CONFIG,
                            now=ANNOUNCED + timedelta(hours=6)) == []


def test_a_figure_that_only_now_arrived_is_not_a_revision():
    """Absent -> present is the hold ending, which task 6 already covers.
    Calling it a revision would report every held print as corrected."""
    assert detect_revisions(a_print(revenue=None), a_print(revenue=9.5e8),
                            CONFIG, now=ANNOUNCED + timedelta(hours=6)) == []


def test_a_figure_that_disappeared_is_a_revision_worth_seeing():
    """Present -> absent means the vendor withdrew a number the ranking used.
    Silently keeping the old one would be the system preferring its own
    memory to its source."""
    revisions = detect_revisions(a_print(eps=0.34), a_print(eps=None), CONFIG,
                                 now=ANNOUNCED + timedelta(hours=6))

    assert [r.field for r in revisions] == ["eps_actual"]
    assert revisions[0].after is None


# --- what it does to the ledger -------------------------------------------

def test_applying_a_revision_appends_and_retires(tmp_path):
    store = a_store(tmp_path)
    store.record_print(a_print(eps=0.34))

    apply_revision(store, a_print(eps=0.42))

    rows = store.prints("cik:1", "2026-Q2")
    assert len(rows) == 2
    assert [r.status for r in rows] == [RowStatus.SUPERSEDED, RowStatus.ACTIVE]
    assert store.active_print("cik:1", "2026-Q2").eps_actual == 0.42
    # The retired row still says what the shortlist was actually built on.
    assert rows[0].eps_actual == 0.34


def test_the_retired_row_can_be_deleted_and_the_active_one_cannot(tmp_path):
    store = a_store(tmp_path)
    store.record_print(a_print(eps=0.34))
    apply_revision(store, a_print(eps=0.42))

    removed = store.delete_superseded_prints("cik:1", "2026-Q2")

    assert removed == 1
    assert [r.status for r in store.prints("cik:1", "2026-Q2")] == [
        RowStatus.ACTIVE]
    assert store.delete_superseded_prints("cik:1", "2026-Q2") == 0


# --- the scan picks it up --------------------------------------------------

class _Calendar:
    def __init__(self, entries):
        self.entries = entries

    def earnings_calendar(self, start, end):
        return self.entries


def _entries(day: date, eps_actual: float) -> list[dict]:
    return [{"symbol": "AMZN", "date": day.isoformat(), "year": 2026,
             "quarter": 2, "epsActual": eps_actual, "epsEstimate": 1.00,
             "revenueActual": 1.05e9, "revenueEstimate": 1.0e9}]


# The scan tests use a wider watch window than production's 48 hours, because
# the entry gates refuse a catalyst that is only hours old and the scan window
# itself starts at the previous business day — so the youngest print a full
# `run_scout` can be driven with is already several days past announcement.
# The 48-hour bound is what `test_a_change_after_the_watch_window...` pins;
# these tests pin the wiring around it.
WIDE = HawkeyeConfig(actual_revision_watch_hours=24 * 30)


def _provider():
    from hawkeye.marketdata.base import StaticProvider
    from tests.conftest import make_bars
    return StaticProvider(bars=make_bars(30, start_price=40.0,
                                         volume=2_000_000),
                          profile_data={"market_cap": 5e9})


def test_a_second_scan_with_a_corrected_actual_revises_and_reports(tmp_path):
    """The scan already re-reads a print on every overlapping window, so the
    second reading is free. What was missing was noticing it differed:
    `_record_print` skipped a repeat outright, which is right for a repeat and
    wrong for a correction."""
    from hawkeye.ledger.stocks import StockStore
    from hawkeye.scout.scout import run_scout

    today = date.today()
    day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))

    run_scout(_Calendar(_entries(day, 1.20)), _provider(), WIDE,
              today=today, stock_store=store)
    result = run_scout(_Calendar(_entries(day, 1.35)), _provider(), WIDE,
                       today=today, stock_store=store)

    assert [(r.ticker, r.field, r.before, r.after) for r in result.revisions] \
        == [("AMZN", "eps_actual", 1.20, 1.35)]
    stock_id = store.stock_by_ticker("AMZN").id
    # A calendar-backed print keeps the calendar's actual in `eps_actual_rows`
    # and leaves `eps_actual` (the chosen vendor's figure) empty — the feed was
    # never asked here. `eps_actual_rows_usable` is the reading that stands.
    assert store.active_print(
        stock_id, "2026-Q2").eps_actual_rows_usable == 1.35
    assert [r.eps_actual_rows_usable
            for r in store.prints(stock_id, "2026-Q2")] == [1.20, 1.35]


def test_the_ranking_is_made_on_the_corrected_figure(tmp_path):
    """Showing the diff is not enough on its own — the point of catching the
    correction during the run is to not rank on a number the vendor has
    already withdrawn."""
    from hawkeye.ledger.stocks import StockStore
    from hawkeye.scout.scout import run_scout

    today = date.today()
    day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))

    first = run_scout(_Calendar(_entries(day, 1.20)), _provider(), WIDE,
                      today=today, stock_store=store)
    second = run_scout(_Calendar(_entries(day, 1.35)), _provider(), WIDE,
                       today=today, stock_store=store)

    assert second.passed[0].quality.eps.actual == 1.35
    assert second.passed[0].score > first.passed[0].score


def test_an_unchanged_second_scan_reports_no_revision(tmp_path):
    from hawkeye.ledger.stocks import StockStore
    from hawkeye.scout.scout import run_scout

    today = date.today()
    day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))

    run_scout(_Calendar(_entries(day, 1.20)), _provider(), WIDE,
              today=today, stock_store=store)
    result = run_scout(_Calendar(_entries(day, 1.20)), _provider(), WIDE,
                       today=today, stock_store=store)

    assert result.revisions == []
    stock_id = store.stock_by_ticker("AMZN").id
    assert len(store.prints(stock_id, "2026-Q2")) == 1


def test_the_report_shows_the_change_before_the_ranking_table(tmp_path):
    """Order matters: a correction noticed after the reader has already read
    the shortlist is a correction they will not act on."""
    from hawkeye.ledger.stocks import StockStore
    from hawkeye.reports.render_ja import render_scout_ja
    from hawkeye.scout.scout import run_scout

    today = date.today()
    day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    run_scout(_Calendar(_entries(day, 1.20)), _provider(), WIDE,
              today=today, stock_store=store)
    result = run_scout(_Calendar(_entries(day, 1.35)), _provider(), WIDE,
                       today=today, stock_store=store)

    text = render_scout_ja(result)

    assert "1.2" in text and "1.35" in text
    assert "実績値が訂正" in text
    assert text.index("実績値が訂正") < text.index("AMZN")


# --- the physical delete is a separate, explicit command -------------------

def test_the_command_previews_retired_rows_and_needs_apply(tmp_path,
                                                           monkeypatch,
                                                           capsys):
    from hawkeye.cli import main

    # The stock master lives in the same database file as the ledger.
    monkeypatch.setenv("HAWKEYE_DB", str(tmp_path / "hawkeye.db"))
    store = StockStore(str(tmp_path / "hawkeye.db"))
    store.put_stock(Stock(id="cik:1", ticker="ADEA", name="Adeia"))
    store.record_print(a_print(eps=0.34))
    apply_revision(store, a_print(eps=0.42))
    store.close()

    assert main(["stocks", "prune-revisions"]) == 0
    preview = capsys.readouterr().out
    assert "0.34" in preview and "--apply" in preview
    assert len(StockStore(str(tmp_path / "hawkeye.db")).prints("cik:1")) == 2

    assert main(["stocks", "prune-revisions", "--apply"]) == 0
    rows = StockStore(str(tmp_path / "hawkeye.db")).prints("cik:1")
    assert [r.status for r in rows] == [RowStatus.ACTIVE]
    assert rows[0].eps_actual == 0.42
