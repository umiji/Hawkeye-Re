"""The scout funnel judging candidates on three legs (§5.3(4)).

Two sources for the EPS actual only exist if both are kept: verification used
to REPLACE the calendar's figures with Yahoo's, which left one reading and no
way to ask whether they agreed. These tests pin the corrected behaviour and
the funnel wiring that depends on it.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    PrintSource,
    SnapshotKind,
    Stock,
)
from hawkeye.ledger.stocks import StockStore
from hawkeye.marketdata.base import StaticProvider
from hawkeye.marketdata.yahoo_earnings import VerifiedEarnings
from hawkeye.scout.earnings import EarningsEvent, parse_calendar, screen_events
from hawkeye.scout.quality import (
    LegStatus,
    assess_event,
    print_from_event,
    reconstructed_consensus,
)
from hawkeye.scout.scout import run_scout
from hawkeye.scout.verify import verify_events
from tests.conftest import make_bars

JST = timezone(timedelta(hours=9))


class FakeCalendar:
    def __init__(self, entries):
        self.entries = entries

    def earnings_calendar(self, start, end):
        return self.entries


class FakeNumbers:
    """Yahoo's reading of a print, injected."""

    def __init__(self, found: dict):
        self.found = found

    def verified_earnings(self, ticker, day):
        return self.found.get(ticker)


def an_event(**overrides) -> EarningsEvent:
    base = dict(ticker="TEST", day=date(2026, 7, 31), eps_actual=1.20,
                eps_estimate=1.00, revenue_actual=1.05e9,
                revenue_estimate=1.0e9, fiscal_quarter="2026-Q2")
    base.update(overrides)
    return EarningsEvent(**base)


# --- keeping both readings -------------------------------------------------

def test_the_calendar_reading_survives_verification():
    """Replacing the calendar's numbers used to discard them. Both are needed
    now: "the sources agree" is the evidence a beat rests on, and it cannot
    be checked against a value that was overwritten."""
    event = an_event(eps_actual=1.91, eps_estimate=1.00)
    screened = screen_events([event], 5.0, 0.0, 0.10, 50.0)
    verified = FakeNumbers({"TEST": VerifiedEarnings(
        ticker="TEST", report_date=date(2026, 7, 31), eps_actual=2.02,
        eps_estimate=1.89, surprise_pct=6.88)})

    out, stats = verify_events([event], screened, verified, limit=5)

    assert stats.verified == 1
    assert out[0].eps_actual == 2.02 and out[0].eps_source == "yahoo"
    assert out[0].calendar_eps_actual == 1.91
    assert out[0].calendar_eps_estimate == 1.00
    assert out[0].eps_estimate == 1.89          # Yahoo's, kept side by side


def test_the_calendar_carries_its_own_fiscal_quarter_label():
    events = parse_calendar([{"symbol": "TEST", "date": "2026-07-31",
                              "year": 2026, "quarter": 2, "epsActual": 1.2,
                              "epsEstimate": 1.0}])
    assert events[0].fiscal_quarter == "2026-Q2"


# --- an event becomes a print + a consensus reading ------------------------

def test_a_verified_event_yields_a_two_source_eps_leg():
    event = an_event(eps_actual=2.02, eps_estimate=1.89, eps_source="yahoo",
                     calendar_eps_actual=2.00, calendar_eps_estimate=1.90)

    quality = assess_event(event, None, _config())

    assert quality.eps.sources == 2
    assert quality.eps.status is LegStatus.BEAT
    assert quality.eps.yahoo_surprise_pct is not None
    assert quality.eps.finnhub_surprise_pct is not None


def test_a_single_source_event_says_so_on_the_leg():
    """One source is now the normal case (2026-08-05: EarningsWhispers is the
    only source of earnings numbers), so this no longer reads as unverified —
    but the leg still carries how thin its consensus was, which is what the
    tribunal and the Japanese report show."""
    quality = assess_event(an_event(), None, _config())

    assert quality.eps.sources == 1
    assert quality.eps.status is LegStatus.BEAT
    assert "single_source_consensus" in quality.eps.flags


def test_the_print_records_which_source_each_actual_came_from():
    row = print_from_event(
        an_event(eps_actual=2.02, eps_source="yahoo", calendar_eps_actual=1.91),
        stock_id="cik:0000320193")

    assert row.eps_actual == 2.02
    assert row.eps_actual_rows == [1.91]
    assert row.source is PrintSource.YAHOO
    assert row.fiscal_quarter == "2026-Q2"


def test_contradictory_actuals_from_the_calendar_reach_the_print():
    """AMZN's print came back as two rows with DIFFERENT actuals (1.88 and
    1.97). Collapsing to one row is right for ranking, but the collapse must
    not hide the contradiction — that is what makes Finnhub's actual unusable
    for this print, and a print row holding only one of them would look like
    a clean single-source reading."""
    rows = [{"symbol": "AMZN", "date": "2026-07-31", "year": 2026,
             "quarter": 2, "epsActual": 1.88, "epsEstimate": 1.83},
            {"symbol": "AMZN", "date": "2026-07-31", "year": 2026,
             "quarter": 1, "epsActual": 1.97, "epsEstimate": 1.83}]

    event = parse_calendar(rows)[0]
    row = print_from_event(event, stock_id="cik:0001018724")

    assert sorted(row.eps_actual_rows) == [1.88, 1.97]
    assert row.eps_actual_rows_usable is None
    quality = assess_event(event, None, _config())
    assert "finnhub_actual_conflict" in quality.eps.flags


def test_a_reconstructed_consensus_says_so():
    snapshot = reconstructed_consensus(
        an_event(eps_source="yahoo", eps_estimate=1.89,
                 calendar_eps_estimate=1.90),
        stock_id="cik:0000320193",
        captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))

    assert snapshot.kind is SnapshotKind.RECONSTRUCTED
    assert snapshot.eps_avg == 1.89 and snapshot.eps_calendar == 1.90
    assert snapshot.eps_analysts is None      # a count exists only pre-print


# --- the funnel ------------------------------------------------------------

def _config():
    from hawkeye.config import HawkeyeConfig
    return HawkeyeConfig()


def _entries(event_day: date) -> list[dict]:
    return [{"symbol": "AMZN", "date": event_day.isoformat(),
             "year": 2026, "quarter": 2,
             "epsActual": 1.20, "epsEstimate": 1.00,
             "revenueActual": 1.05e9, "revenueEstimate": 1.0e9}]


def _provider() -> StaticProvider:
    return StaticProvider(bars=make_bars(30, start_price=40.0,
                                         volume=2_000_000),
                          profile_data={"market_cap": 5e9})


def test_the_funnel_records_the_stock_the_print_and_the_consensus(tmp_path):
    today = date.today()
    event_day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))

    result = run_scout(FakeCalendar(_entries(event_day)), _provider(),
                       _config(), today=today, stock_store=store)

    assert [c.ticker for c in result.passed] == ["AMZN"]
    stock = store.stock_by_ticker("AMZN")
    assert stock is not None
    row = store.active_print(stock.id, "2026-Q2")
    assert row is not None and row.report_date == event_day
    assert row.consensus_snapshot_id                      # pinned, not copied
    assert store.consensus(row.consensus_snapshot_id).kind \
        is SnapshotKind.RECONSTRUCTED


def test_a_pre_registered_consensus_is_used_instead_of_reconstructing_one(tmp_path):
    """The whole point of capturing early: once a pre-registered row exists,
    the print is judged against what was expected BEFORE it, not against
    whatever the calendar says afterwards."""
    today = date.today()
    event_day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    stock_id = store.put_stock(Stock(cik="0001018724", ticker="AMZN"))
    pre_registered = store.capture_consensus(ConsensusSnapshot(
        stock_id=stock_id, ticker="AMZN", fiscal_quarter="2026-Q2",
        captured_at=datetime.combine(event_day - timedelta(days=1),
                                     datetime.min.time(), tzinfo=JST),
        kind=SnapshotKind.PRE_REGISTERED, eps_avg=1.00, eps_calendar=1.00,
        eps_analysts=25, revenue_avg=1.0e9, revenue_calendar=1.0e9,
        revenue_analysts=20))

    result = run_scout(FakeCalendar(_entries(event_day)), _provider(),
                       _config(), today=today, stock_store=store)

    row = store.active_print(stock_id, "2026-Q2")
    assert row.consensus_snapshot_id == pre_registered
    candidate = result.passed[0]
    assert candidate.quality is not None
    assert candidate.quality.eps.status is LegStatus.BEAT
    assert candidate.quality.revenue.status is LegStatus.BEAT
    assert "EPS" in candidate.brief.catalyst.description


def test_scanning_the_same_print_again_does_not_error(tmp_path):
    """Windows overlap by design. A second scan must not fail on the
    append-only index, and must not invent a second reading of one print."""
    today = date.today()
    event_day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))

    run_scout(FakeCalendar(_entries(event_day)), _provider(), _config(),
              today=today, stock_store=store)
    run_scout(FakeCalendar(_entries(event_day)), _provider(), _config(),
              today=today, stock_store=store)

    stock = store.stock_by_ticker("AMZN")
    assert len(store.prints(stock.id, "2026-Q2")) == 1


def test_an_unconfirmable_beat_cannot_outrank_a_confirmed_one(tmp_path):
    """The ranking has to prefer the name whose beat two sources agree on,
    even when the unconfirmed one shows a larger percentage — that is the
    2026-08-01 defect (rank by the biggest number) stated in the new terms."""
    today = date.today()
    event_day = today - timedelta(days=3)
    entries = [
        {"symbol": "CONF", "date": event_day.isoformat(), "year": 2026,
         "quarter": 2, "epsActual": 1.20, "epsEstimate": 1.00,
         "revenueActual": 1.05e9, "revenueEstimate": 1.0e9},
        {"symbol": "THIN", "date": event_day.isoformat(), "year": 2026,
         "quarter": 2, "epsActual": 3.00, "epsEstimate": 1.00},
    ]
    store = StockStore(str(tmp_path / "hawkeye.db"))
    thin = store.put_stock(Stock(ticker="THIN"))
    store.capture_consensus(ConsensusSnapshot(
        stock_id=thin, ticker="THIN", fiscal_quarter="2026-Q2",
        captured_at=datetime.combine(event_day - timedelta(days=1),
                                     datetime.min.time(), tzinfo=JST),
        kind=SnapshotKind.PRE_REGISTERED, eps_avg=1.00, eps_calendar=1.00,
        eps_analysts=1))                       # one analyst: unconfirmable
    conf = store.put_stock(Stock(ticker="CONF"))
    store.capture_consensus(ConsensusSnapshot(
        stock_id=conf, ticker="CONF", fiscal_quarter="2026-Q2",
        captured_at=datetime.combine(event_day - timedelta(days=1),
                                     datetime.min.time(), tzinfo=JST),
        kind=SnapshotKind.PRE_REGISTERED, eps_avg=1.00, eps_calendar=1.00,
        eps_analysts=25, revenue_avg=1.0e9, revenue_calendar=1.0e9,
        revenue_analysts=20))

    result = run_scout(FakeCalendar(entries), _provider(), _config(),
                       today=today, stock_store=store)

    assert [c.ticker for c in result.passed] == ["CONF", "THIN"]
    assert result.passed[1].quality.eps.status is LegStatus.UNVERIFIED


def test_the_funnel_can_fill_the_guidance_yardstick_after_the_print(tmp_path):
    """Guidance is only judgeable against next quarter's consensus, and after
    a print that is the `0q` row (see test_consensus_capture). Without this
    the funnel would record every guidance leg as "absent", which describes
    our data collection rather than the company."""
    from hawkeye.marketdata.consensus import ConsensusReading

    today = date.today()
    event_day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))

    class PostPrintConsensus:
        def consensus(self, ticker):
            return ConsensusReading(eps_avg=2.44, revenue_avg=2.4e11,
                                    eps_analysts=43)

    run_scout(FakeCalendar(_entries(event_day)), _provider(), _config(),
              today=today, stock_store=store,
              consensus_source=PostPrintConsensus())

    stock = store.stock_by_ticker("AMZN")
    snapshot = store.consensus_in_force(stock.id, "2026-Q2")
    assert snapshot.next_quarter_eps_avg == 2.44
    assert snapshot.next_quarter_revenue_avg == 2.4e11
    # ...and it must NOT be mistaken for this quarter's own consensus
    assert snapshot.eps_avg != 2.44


def test_the_screen_still_works_without_a_store():
    """The store is optional so every existing caller — and the offline test
    suite — keeps working unchanged."""
    today = date.today()
    event_day = today - timedelta(days=3)
    result = run_scout(FakeCalendar(_entries(event_day)), _provider(),
                       _config(), today=today)
    assert [c.ticker for c in result.passed] == ["AMZN"]
    assert result.passed[0].quality is None
