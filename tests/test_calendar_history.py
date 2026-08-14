"""An unbroken quarterly history for every name that entered the funnel
(docs/design/MASTER_OVERVIEW.ja.md §6.1(C)).

Until now a quarter was recorded only for candidates that reached enrichment.
A stock picked in Q1, screened out in Q2 and Q3, then picked again in Q4 had
a hole exactly where the comparison would go — and the hole is indistinguish-
able from "that quarter was never reported". The calendar response already
contains the numbers, so filling it costs no API call: the row is written at
the shallowest depth, `calendar_only`, which says precisely how hard anyone
looked.

Two boundaries this pins:

- a name we have never seen AND that failed the screen is not recorded, or
  the master would fill up with the entire earnings calendar;
- the pass never replaces a deeper reading with a shallower one.
"""
from __future__ import annotations

import dataclasses
from datetime import date, timedelta

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.stocks import PrintSource, Stock
from hawkeye.ledger.stocks import StockStore
from hawkeye.marketdata.base import StaticProvider
from hawkeye.scout.scout import run_scout
from tests.conftest import make_bars


class FakeCalendar:
    def __init__(self, entries):
        self.entries = entries

    def earnings_calendar(self, start, end):
        return self.entries


def _config(**overrides) -> HawkeyeConfig:
    return dataclasses.replace(HawkeyeConfig(), **overrides)


def _provider() -> StaticProvider:
    return StaticProvider(bars=make_bars(30, start_price=40.0,
                                         volume=2_000_000),
                          profile_data={"market_cap": 5e9})


def _entry(ticker: str, day: date, actual: float = 1.20,
           estimate: float = 1.00) -> dict:
    return {"symbol": ticker, "date": day.isoformat(), "year": 2026,
            "quarter": 2, "epsActual": actual, "epsEstimate": estimate,
            "revenueActual": 1.05e9, "revenueEstimate": 1.0e9}


def _day() -> date:
    return date.today() - timedelta(days=3)


def test_a_screened_name_that_never_reached_enrichment_still_gets_its_quarter(
        tmp_path):
    """The enrichment budget decides who is argued about, not whose history
    exists. A name dropped at the cap is one we chose not to pay for — that
    is a fact about the run, not about the company's quarter."""
    day = _day()
    store = StockStore(str(tmp_path / "hawkeye.db"))

    result = run_scout(FakeCalendar([_entry("AAA", day), _entry("BBB", day)]),
                       _provider(), _config(scout_max_enrich=1),
                       today=date.today(), stock_store=store)

    assert [c.ticker for c in result.capped] == ["BBB"]
    capped = store.stock_by_ticker("BBB")
    row = store.active_print(capped.id, "2026-Q2")
    assert row is not None
    assert row.source is PrintSource.FINNHUB
    assert row.eps_actual_rows == [1.20]


def test_the_quarter_keeps_the_consensus_it_was_measured_against(tmp_path):
    """A row of actuals with no estimate beside it cannot be judged later.
    Both numbers came from the same free calendar response."""
    day = _day()
    store = StockStore(str(tmp_path / "hawkeye.db"))

    run_scout(FakeCalendar([_entry("AAA", day), _entry("BBB", day)]),
              _provider(), _config(scout_max_enrich=1), today=date.today(),
              stock_store=store)

    capped = store.stock_by_ticker("BBB")
    row = store.active_print(capped.id, "2026-Q2")
    assert row.consensus_snapshot_id
    assert store.consensus(row.consensus_snapshot_id).eps_calendar == 1.00


def test_a_known_stock_keeps_the_quarter_it_failed_the_screen_on(tmp_path):
    """The gap this closes: a name we already follow reports an ordinary
    quarter, the 5% screen drops it, and its history loses a quarter."""
    day = _day()
    store = StockStore(str(tmp_path / "hawkeye.db"))
    known = store.put_stock(Stock(ticker="QUIET"))

    run_scout(FakeCalendar([_entry("QUIET", day, actual=1.01,
                                   estimate=1.00)]),
              _provider(), _config(), today=date.today(), stock_store=store)

    row = store.active_print(known, "2026-Q2")
    assert row is not None and row.source is PrintSource.FINNHUB


def test_a_stranger_that_failed_the_screen_is_not_added_to_the_master(tmp_path):
    """Otherwise every scan would import the whole earnings calendar into the
    master, and "stocks we follow" would stop meaning anything."""
    day = _day()
    store = StockStore(str(tmp_path / "hawkeye.db"))

    run_scout(FakeCalendar([_entry("NOBODY", day, actual=1.01,
                                   estimate=1.00)]),
              _provider(), _config(), today=date.today(), stock_store=store)

    assert store.stock_by_ticker("NOBODY") is None


def test_the_history_pass_never_shallows_a_deeper_reading(tmp_path):
    """The enriched name already has the deeper row. Appending a
    `calendar_only` reading of the same quarter afterwards would make the
    history say we looked less hard than we did."""
    day = _day()
    store = StockStore(str(tmp_path / "hawkeye.db"))

    run_scout(FakeCalendar([_entry("AAA", day)]), _provider(), _config(),
              today=date.today(), stock_store=store)

    stock = store.stock_by_ticker("AAA")
    assert len(store.prints(stock.id, "2026-Q2")) == 1


def test_re_scanning_the_same_window_does_not_duplicate_the_history(tmp_path):
    """Scan windows overlap by design, and the table is append-only: a second
    row at the same depth is refused at the SQLite level, so a repeat would
    raise rather than be ignored."""
    day = _day()
    store = StockStore(str(tmp_path / "hawkeye.db"))
    entries = [_entry("AAA", day), _entry("BBB", day)]

    run_scout(FakeCalendar(entries), _provider(), _config(scout_max_enrich=1),
              today=date.today(), stock_store=store)
    run_scout(FakeCalendar(entries), _provider(), _config(scout_max_enrich=1),
              today=date.today(), stock_store=store)

    capped = store.stock_by_ticker("BBB")
    assert len(store.prints(capped.id, "2026-Q2")) == 1
