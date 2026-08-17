"""Judging ONE named stock's latest quarter, outside the discovery screen.

The 5% surprise screen exists to decide who is worth looking at when nobody
asked. When a person names a stock, that question is already answered — and
applying the screen anyway would refuse to judge exactly the case the whole
three-leg design was built from: AMZN's calendar rows collapse to +2.7%, so
the screen drops it before the feed is ever read, and the print that started
this investigation would be unjudgeable in the product that judges prints.

Same code path as the funnel otherwise: same earnings feed, same
one-vendor-per-print rule, same pinned consensus, same recorded quarter.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import new_id
from hawkeye.contracts.stocks import (ConsensusSnapshot, GuidanceReading,
                                      SnapshotKind, Stock)
from hawkeye.ledger.stocks import StockStore
from tests.conftest import FakeWhispers, make_whispers

from hawkeye.scout.quality import LegStatus
from hawkeye.scout.single import StoredPrintMismatch, judge_ticker

JST = timezone(timedelta(hours=9))


class FakeCalendar:
    def __init__(self, entries):
        self.entries = entries
        self.windows: list[tuple[date, date]] = []

    def earnings_calendar(self, start, end):
        self.windows.append((start, end))
        return self.entries


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


def test_the_earnings_feed_is_read_for_a_named_stock_too(tmp_path):
    """The feed is what makes a print judgeable at all when the calendar
    contradicts itself. Skipping it here would judge a hand-picked stock on
    weaker evidence than a discovered one, with nothing in the record to
    say so."""
    day = date(2026, 7, 31)
    store = StockStore(str(tmp_path / "hawkeye.db"))

    judged = judge_ticker(
        "AMZN", FakeCalendar(_amzn_rows(day)), _config(), report_date=day,
        stock_store=store,
        numbers_source=FakeWhispers({"AMZN": make_whispers(
            "AMZN", announced=day, eps_actual=5.75, eps_consensus=1.83,
            revenue_actual=1.68e11, revenue_consensus=1.62e11)}))

    assert judged.event.numbers_source == "whispers"
    assert judged.quality.eps.source == "whispers"
    # The calendar's own contradiction is still on the row, and still named —
    # it just no longer decides anything, because the reading stands on
    # figures the calendar never touched.
    assert "finnhub_actual_conflict" in judged.quality.eps.flags
    assert judged.quality.eps.status is LegStatus.BEAT


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


# --- the quarter already on record (T-006) ---------------------------------

def _amzn_feed(day: date, **overrides) -> FakeWhispers:
    fields = dict(eps_actual=5.75, eps_consensus=1.83,
                  revenue_actual=1.68e11, revenue_consensus=1.62e11)
    fields.update(overrides)
    return FakeWhispers({"AMZN": make_whispers("AMZN", announced=day,
                                               **fields)})


def _judge(store, day: date, feed: FakeWhispers):
    return judge_ticker("AMZN", FakeCalendar(_amzn_rows(day)), _config(),
                        report_date=day, stock_store=store,
                        numbers_source=feed)


def test_a_stored_reading_reaches_a_named_stocks_catalyst_text(tmp_path):
    """The scan reads the company's own outlook and its account of the
    quarter and stores both on the active print row. Rebuilding the row from
    the calendar threw both away, so the tribunal was told 'guidance not
    disclosed' about a company the ranking had just scored on its guidance
    (T-006, seen live on HLIT/SDRL 2026-08-17)."""
    day = date(2026, 7, 31)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    stock_id = store.put_stock(Stock(cik="0001018724", ticker="AMZN"))
    store.capture_consensus(ConsensusSnapshot(
        stock_id=stock_id, ticker="AMZN", fiscal_quarter="2026-Q2",
        captured_at=datetime.combine(day - timedelta(days=1),
                                     datetime.min.time(), tzinfo=JST),
        kind=SnapshotKind.PRE_REGISTERED, eps_avg=1.83, eps_calendar=1.83,
        revenue_avg=1.62e11, next_quarter_eps_avg=1.90))
    feed = _amzn_feed(day)
    _judge(store, day, feed)          # the scan's own recording of the quarter
    # The two agent readings land afterwards, exactly as `guidance submit` /
    # `cause submit` put them there: a revised row carrying the readings.
    active = store.active_print(stock_id, "2026-Q2")
    store.revise_print(active.model_copy(update={
        "id": new_id("ern"),
        "guidance": GuidanceReading(period="2026-Q3", eps_low=2.0,
                                    eps_high=2.2, extractor="agent",
                                    source_excerpt="sees Q3 EPS of $2.00-$2.20"),
        "guidance_reason": "",
        "cause_reason": "no_cause_in_source"}))

    judged = _judge(store, day, feed)

    text = judged.catalyst_description
    assert "Guidance beat consensus" in text
    assert "guidance_not_published" not in text
    assert "the source states none" in text
    assert "it has not been read yet" not in text


def test_a_restated_figure_stops_the_judgment_instead_of_picking_a_side(tmp_path):
    """When the quarter on record and the fresh fetch disagree on a reported
    figure, either side could be the corrected one — so nothing here decides.
    The judgment refuses, names the figures, and a human chooses (User
    decision, 2026-08-17)."""
    day = date(2026, 7, 31)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    first = _judge(store, day, _amzn_feed(day))

    with pytest.raises(StoredPrintMismatch) as caught:
        _judge(store, day, _amzn_feed(day, eps_actual=6.10))

    mismatch = caught.value
    assert mismatch.ticker == "AMZN"
    assert mismatch.fiscal_quarter == "2026-Q2"
    assert {d.field: (d.stored, d.fetched) for d in mismatch.differences} == {
        "eps_actual": (5.75, 6.10)}
    # The row on record was not touched: refusing is not revising.
    row = store.active_print(first.stock_id, "2026-Q2")
    assert row.eps_actual == 5.75


class FakeFinnhub(FakeCalendar):
    available = True


def test_case_open_reports_the_difference_and_opens_no_case(
        tmp_path, monkeypatch, capsys):
    """What the operator actually sees: `hawkeye case open --from-earnings`
    prints both readings of the figure and exits without creating a case."""
    import hawkeye.cli as cli

    day = date(2026, 7, 31)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    _judge(store, day, _amzn_feed(day))
    monkeypatch.setattr(cli, "FinnhubProvider",
                        lambda: FakeFinnhub(_amzn_rows(day)))
    monkeypatch.setattr(cli, "WhispersSource",
                        lambda: _amzn_feed(day, eps_actual=6.10))
    monkeypatch.setattr(cli, "_stock_store", lambda: store)
    monkeypatch.setattr(cli, "EdgarDirectory", lambda: None)

    def _no_case(*args, **kwargs):
        raise AssertionError("a mismatched print must not open a case")
    monkeypatch.setattr(cli.casefile, "open_case", _no_case)

    rc = cli.main(["case", "open", "AMZN", "--from-earnings",
                   "--event-date", day.isoformat(), "--nav", "10000"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "eps_actual" in err
    assert "5.75" in err and "6.1" in err
