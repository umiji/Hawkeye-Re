"""Judging ONE named stock's latest quarter, outside the discovery screen.

The 5% surprise screen exists to decide who is worth looking at when nobody
asked. When a person names a stock, that question is already answered — and
applying the screen anyway would refuse to judge exactly the case the whole
three-leg design was built from: AMZN's calendar rows collapse to +2.7%, so
the screen drops it before verification ever runs, and the print that started
this investigation would be unjudgeable in the product that judges prints.

Same code path as the funnel otherwise: same verification, same both-sources
rule, same pinned consensus, same recorded quarter.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.stocks import ConsensusSnapshot, SnapshotKind, Stock
from hawkeye.ledger.stocks import StockStore
from hawkeye.marketdata.yahoo_earnings import VerifiedEarnings
from hawkeye.scout.quality import LegStatus
from hawkeye.scout.single import judge_ticker

JST = timezone(timedelta(hours=9))


class FakeCalendar:
    def __init__(self, entries):
        self.entries = entries
        self.windows: list[tuple[date, date]] = []

    def earnings_calendar(self, start, end):
        self.windows.append((start, end))
        return self.entries


class FakeNumbers:
    def __init__(self, found: dict):
        self.found = found

    def verified_earnings(self, ticker, day):
        return self.found.get(ticker)


def _config() -> HawkeyeConfig:
    return HawkeyeConfig()


def _amzn_rows(day: date) -> list[dict]:
    """The real shape: two rows for one print, contradicting each other, and
    a collapsed reading of +2.7% that the discovery screen would drop."""
    return [{"symbol": "AMZN", "date": day.isoformat(), "year": 2026,
             "quarter": 2, "epsActual": 1.88, "epsEstimate": 1.83,
             "revenueActual": 1.68e11, "revenueEstimate": 1.62e11},
            {"symbol": "AMZN", "date": day.isoformat(), "year": 2026,
             "quarter": 1, "epsActual": 1.97, "epsEstimate": 1.83}]


def test_a_named_stock_is_judged_even_though_the_screen_would_drop_it(tmp_path):
    day = date(2026, 7, 31)
    store = StockStore(str(tmp_path / "hawkeye.db"))

    judged = judge_ticker("AMZN", FakeCalendar(_amzn_rows(day)), _config(),
                          report_date=day, stock_store=store)

    assert judged is not None
    assert judged.quality.ticker == "AMZN"
    assert judged.quality.fiscal_quarter == "2026-Q2"
    # Finnhub contradicts itself on this print, so its actual is unusable —
    # the leg says so instead of picking whichever row looks right.
    assert "finnhub_actual_conflict" in judged.quality.eps.flags


def test_the_second_source_is_read_for_a_named_stock_too(tmp_path):
    """Verification is what gives the beat rule two opinions. Skipping it
    here would make a hand-picked stock judged on weaker evidence than a
    discovered one, with nothing in the record to say so."""
    day = date(2026, 7, 31)
    store = StockStore(str(tmp_path / "hawkeye.db"))

    judged = judge_ticker(
        "AMZN", FakeCalendar(_amzn_rows(day)), _config(), report_date=day,
        stock_store=store,
        numbers_source=FakeNumbers({"AMZN": VerifiedEarnings(
            ticker="AMZN", report_date=day, eps_actual=5.75,
            eps_estimate=1.83, surprise_pct=214.2)}))

    assert judged.event.eps_source == "yahoo"
    assert judged.quality.eps.yahoo_surprise_pct is not None


def test_the_pre_registered_consensus_is_what_it_is_judged_against(tmp_path):
    day = date(2026, 7, 31)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    stock_id = store.put_stock(Stock(cik="0001018724", ticker="AMZN"))
    pinned = store.capture_consensus(ConsensusSnapshot(
        stock_id=stock_id, ticker="AMZN", fiscal_quarter="2026-Q2",
        captured_at=datetime.combine(day - timedelta(days=1),
                                     datetime.min.time(), tzinfo=JST),
        kind=SnapshotKind.PRE_REGISTERED, eps_avg=1.83, eps_calendar=1.83,
        eps_analysts=43, revenue_avg=1.62e11, revenue_calendar=1.62e11,
        revenue_analysts=38))

    judged = judge_ticker("AMZN", FakeCalendar(_amzn_rows(day)), _config(),
                          report_date=day, stock_store=store)

    assert judged.consensus_id == pinned
    row = store.active_print(stock_id, "2026-Q2")
    assert row is not None and row.consensus_snapshot_id == pinned


def test_the_catalyst_text_is_the_three_leg_reading(tmp_path):
    """What the tribunal argues over. A hand-picked stock must arrive with
    the same structured reading a discovered one does — otherwise 'judge this
    name' quietly means 'judge it on prose'."""
    day = date(2026, 7, 31)
    store = StockStore(str(tmp_path / "hawkeye.db"))

    judged = judge_ticker("AMZN", FakeCalendar(_amzn_rows(day)), _config(),
                          report_date=day, stock_store=store)

    assert "three legs" in judged.catalyst_description
    assert "UNVERIFIED" in judged.catalyst_description


def test_a_ticker_with_no_reported_print_is_reported_as_such(tmp_path):
    store = StockStore(str(tmp_path / "hawkeye.db"))

    assert judge_ticker("NOSUCH", FakeCalendar([]), _config(),
                        report_date=date(2026, 7, 31),
                        stock_store=store) is None


def test_the_calendar_is_asked_around_the_named_day(tmp_path):
    """A print files under the session it belongs to, which is not always the
    day the wire crossed; an exact-date query reads as 'no such print'."""
    day = date(2026, 7, 31)
    calendar = FakeCalendar(_amzn_rows(day))

    judge_ticker("AMZN", calendar, _config(), report_date=day,
                 stock_store=StockStore(str(tmp_path / "hawkeye.db")))

    start, end = calendar.windows[0]
    assert start < day < end
