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
from hawkeye.scout.earnings import EarningsEvent, parse_calendar, screen_events
from hawkeye.scout.quality import (
    LegStatus,
    assess_event,
    print_from_event,
    reconstructed_consensus,
)
from hawkeye.scout import guidance_case
from hawkeye.scout.guidance_agent import parse_reply
from hawkeye.scout.scout import rerank_after_guidance, run_scout
from hawkeye.scout.numbers import read_numbers
from tests.conftest import FakeWhispers, make_bars, make_whispers

JST = timezone(timedelta(hours=9))


class FakeCalendar:
    def __init__(self, entries):
        self.entries = entries

    def earnings_calendar(self, start, end):
        return self.entries


def an_event(**overrides) -> EarningsEvent:
    base = dict(ticker="TEST", day=date(2026, 7, 31), eps_actual=1.20,
                eps_estimate=1.00, revenue_actual=1.05e9,
                revenue_estimate=1.0e9, fiscal_quarter="2026-Q2")
    base.update(overrides)
    return EarningsEvent(**base)


# --- keeping both readings -------------------------------------------------

def test_the_calendar_reading_survives_the_feeds_substitution():
    """Replacing the calendar's numbers used to discard them. Keeping both is
    what makes "how far do the two vendors disagree" a measurement this
    system can take, and it is impossible against an overwritten value."""
    event = an_event(eps_actual=1.91, eps_estimate=1.00)
    screened = screen_events([event], 5.0, 0.0, 0.10, 50.0)
    feed = FakeWhispers({"TEST": make_whispers(
        "TEST", eps_actual=2.02, eps_consensus=1.89)})

    out, stats = read_numbers([event], screened, feed, limit=5)

    assert stats.from_whispers == 1
    assert out[0].eps_actual == 2.02 and out[0].numbers_source == "whispers"
    assert out[0].calendar_eps_actual == 1.91
    assert out[0].calendar_eps_estimate == 1.00
    assert out[0].eps_estimate == 1.89          # the feed's, kept side by side


def test_the_calendar_carries_its_own_fiscal_quarter_label():
    events = parse_calendar([{"symbol": "TEST", "date": "2026-07-31",
                              "year": 2026, "quarter": 2, "epsActual": 1.2,
                              "epsEstimate": 1.0}])
    assert events[0].fiscal_quarter == "2026-Q2"


# --- an event becomes a print + a consensus reading ------------------------

def test_a_feed_backed_event_is_judged_on_the_feeds_own_pair():
    """Both figures from one vendor. The calendar's actual sits beside the
    reading and is named, but it is not what the percentage is built from."""
    event = an_event(eps_actual=2.02, eps_estimate=1.89,
                     numbers_source="whispers",
                     calendar_eps_actual=2.00, calendar_eps_estimate=1.90)

    quality = assess_event(event, None, _config())

    assert quality.eps.source == "whispers"
    assert quality.eps.status is LegStatus.BEAT
    assert quality.eps.actual == 2.02 and quality.eps.estimate == 1.89
    assert quality.eps.other_actual == 2.00        # recorded, never an input


def test_a_calendar_only_event_is_judged_on_the_calendars_own_pair():
    """The feed declining is the normal fallback, not a failure: the whole
    print moves to the calendar rather than half of it."""
    quality = assess_event(an_event(), None, _config())

    assert quality.eps.source == "finnhub"
    assert quality.eps.status is LegStatus.BEAT
    assert quality.eps.actual == 1.20 and quality.eps.estimate == 1.00


def test_the_print_records_which_source_each_actual_came_from():
    row = print_from_event(
        an_event(eps_actual=2.02, numbers_source="whispers",
                 calendar_eps_actual=1.91),
        stock_id="cik:0000320193")

    assert row.eps_actual == 2.02
    assert row.eps_actual_rows == [1.91]
    assert row.source is PrintSource.WHISPERS
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
        an_event(numbers_source="whispers", eps_estimate=1.89,
                 calendar_eps_estimate=1.90, revenue_estimate=1.05e9,
                 calendar_revenue_estimate=1.0e9),
        stock_id="cik:0000320193",
        captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))

    assert snapshot.kind is SnapshotKind.RECONSTRUCTED
    assert snapshot.eps_avg == 1.89 and snapshot.eps_calendar == 1.90
    # Revenue moves with EPS. Leaving the feed's revenue estimate in the
    # calendar's field would rebuild the cross-vendor ratio one field lower.
    assert snapshot.revenue_avg == 1.05e9
    assert snapshot.revenue_calendar == 1.0e9
    assert snapshot.eps_analysts is None      # a count exists only pre-print


def test_the_full_year_yardstick_reaches_the_snapshot_that_judges_guidance():
    """The full-year consensus is read off the print's own summary, so the
    reconstruction is where it has to land — it is what a full-year guidance
    is measured against, and without it that guidance reads as absent."""
    snapshot = reconstructed_consensus(
        an_event(numbers_source="whispers", full_year_eps_estimate=4.76,
                 full_year_revenue_estimate=2.08e9,
                 full_year_period="FY2026"),
        stock_id="cik:0000007084")

    assert snapshot.full_year_eps_avg == 4.76
    assert snapshot.full_year_revenue_avg == 2.08e9
    assert snapshot.full_year_period == "FY2026"


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


_QUARTERLY_GUIDANCE = (
    "The company said it expects third quarter earnings of $2.50 to $2.70 "
    "per share. The current consensus earnings estimate is $2.44 per share "
    "for the quarter ending September 30, 2026.")
_FULL_YEAR_GUIDANCE = (
    "The company said it expects 2026 earnings of $5.15 to $5.60 per share. "
    "The current consensus earnings estimate is $4.76 per share for the year "
    "ending December 31, 2026.")


def _feed(event_day: date, summary: str) -> FakeWhispers:
    return FakeWhispers({"AMZN": make_whispers(
        "AMZN", announced=event_day, summary=summary)})


def _read_staged_guidance(store, reply: dict) -> None:
    """Stand in for `hawkeye guidance queue` + `submit`: attach exactly the
    reply a scan staged, to the row the scan wrote for it."""
    cases = guidance_case.list_cases()
    assert len(cases) == 1
    case = cases[0]
    extraction = parse_reply(reply, case.request(), model="test-model")
    guidance_case.attach(store, case, extraction)
    guidance_case.discard(case.id)


def _pre_register(store, stock_id: str, event_day: date) -> str:
    """A row captured the day before the print, with no yardstick on it —
    which is every row the pre-registration pass can write, because the
    forward endpoint states this quarter's consensus and nothing beyond it."""
    return store.capture_consensus(ConsensusSnapshot(
        stock_id=stock_id, ticker="AMZN", fiscal_quarter="2026-Q2",
        captured_at=datetime.combine(event_day - timedelta(days=1),
                                     datetime.min.time(), tzinfo=JST),
        kind=SnapshotKind.PRE_REGISTERED, eps_avg=1.00, eps_calendar=1.00,
        eps_analysts=25, revenue_avg=1.0e9, revenue_calendar=1.0e9,
        revenue_analysts=20))


def test_a_pre_registered_row_does_not_block_the_full_year_guidance_leg(tmp_path):
    """The full-year yardstick only exists in the summary the print itself
    carries, so a row captured BEFORE the print can never hold one. Judging
    against the pre-registered row alone therefore recorded every
    pre-registered name as having guided nothing — the company's own outlook
    thrown away because of when the bar was captured."""
    today = date.today()
    event_day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    stock_id = store.put_stock(Stock(cik="0001018724", ticker="AMZN"))
    pinned = _pre_register(store, stock_id, event_day)

    result = run_scout(FakeCalendar(_entries(event_day)), _provider(),
                       _config(), today=today, stock_store=store,
                       numbers_source=_feed(event_day, _FULL_YEAR_GUIDANCE))
    _read_staged_guidance(store, {
        "guided": True, "period": "FY2026",
        "eps_low": 5.15, "eps_high": 5.60,
        "quote": "2026 earnings of $5.15 to $5.60 per share"})
    rerank_after_guidance(store, result, _config())

    guidance = result.passed[0].quality.guidance
    assert guidance.status is LegStatus.BEAT
    assert guidance.estimate == 4.76
    # The pre-registered row is still what the surprise ratio stands on, and
    # it is still exactly as it was written.
    row = store.active_print(stock_id, "2026-Q2")
    assert row.consensus_snapshot_id == pinned
    assert store.consensus(pinned).full_year_eps_avg is None


def test_the_quarterly_yardstick_comes_from_the_print_that_carried_the_guidance(
        tmp_path):
    """One sentence gives both the outlook and the bar it is measured against,
    so they cannot be out of step. The bar used to come from a separate lookup
    taken days later, against a guidance read here."""
    today = date.today()
    event_day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))

    result = run_scout(FakeCalendar(_entries(event_day)), _provider(),
                       _config(), today=today, stock_store=store,
                       numbers_source=_feed(event_day, _QUARTERLY_GUIDANCE))
    _read_staged_guidance(store, {
        "guided": True, "period": "2026-Q3",
        "eps_low": 2.50, "eps_high": 2.70,
        "quote": "third quarter earnings of $2.50 to $2.70 per share"})
    rerank_after_guidance(store, result, _config())

    guidance = result.passed[0].quality.guidance
    assert guidance.status is LegStatus.BEAT
    assert guidance.estimate == 2.44
    stock = store.stock_by_ticker("AMZN")
    snapshot = store.consensus_in_force(stock.id, "2026-Q2")
    # Recorded, not only used: a reconstruction is written after the print, so
    # the bar the judgment stood on stays readable in the ledger.
    assert snapshot.next_quarter_eps_avg == 2.44
    assert snapshot.eps_avg != 2.44        # never this quarter's own consensus


# The guidance yardstick used to come from a second vendor read days after
# the print (Yahoo's `0q` row, re-labelled by `shift_after_print`). It now
# comes out of the summary the print itself carried — same string, same
# moment, no extra request — which the two tests above pin.


def test_the_screen_still_works_without_a_store():
    """The store is optional so every existing caller — and the offline test
    suite — keeps working unchanged."""
    today = date.today()
    event_day = today - timedelta(days=3)
    result = run_scout(FakeCalendar(_entries(event_day)), _provider(),
                       _config(), today=today)
    assert [c.ticker for c in result.passed] == ["AMZN"]
    assert result.passed[0].quality is None


# --- the guidance an agent read, and re-scored after the fact --------------
#
# The third leg is the only one that arrives as prose, and since 2026-08-10 an
# agent reads it rather than a pattern. Nothing inside a scan process can call
# that agent (docs/design/RANK_AFTER_GUIDANCE.ja.md): every scan stages the
# sentence to `var/guidance/` and moves on, so what these tests pin is the
# STAGING contract — the sentence the queued case carries, the row it writes
# carrying who eventually read it — and the RE-SCORING contract:
# `rerank_after_guidance` has to pick up whatever `hawkeye guidance submit`
# attached and fold it into the score `hawkeye rank` records.

_SUMMARY = (
    "Test Corp reported second quarter earnings of $1.20 per share. "
    "The company said it expects third quarter results to range from a loss "
    "of $1.00 per share to breakeven. The current consensus estimate is "
    "earnings of $0.08 per share for the quarter ending September 30, 2026.")


def _scan(tmp_path, monkeypatch, summary=_SUMMARY):
    today = date.today()
    event_day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    result = run_scout(FakeCalendar(_entries(event_day)), _provider(),
                       _config(), today=today, stock_store=store,
                       numbers_source=_feed(event_day, summary))
    return result, store


def test_the_staged_case_carries_the_sentence_and_the_quarter_that_follows(
        tmp_path, monkeypatch):
    _scan(tmp_path, monkeypatch)

    cases = guidance_case.list_cases()
    assert len(cases) == 1
    assert cases[0].summary == _SUMMARY
    assert cases[0].request().next_quarter == "2026-Q3"


def test_a_reading_no_pattern_could_produce_reaches_the_print_row(tmp_path, monkeypatch):
    """"a loss of $1.00 per share to breakeven" is -1.00 to 0.00. The
    regular expression that used to read this leg refused it, and refusing
    it is what layer 2 exists to stop."""
    _, store = _scan(tmp_path, monkeypatch)

    _read_staged_guidance(store, {
        "guided": True, "period": "2026-Q3", "eps_low": -1.00, "eps_high": 0.0,
        "quote": ("third quarter results to range from a loss of $1.00 per "
                  "share to breakeven")})

    row = store.active_print(store.stock_by_ticker("AMZN").id, "2026-Q2")
    assert row.guidance is not None
    assert row.guidance.eps_low == -1.00 and row.guidance.eps_high == 0.0
    assert row.guidance.extractor == "agent"
    assert row.guidance.extractor_model == "test-model"


def test_attaching_staged_guidance_retires_the_pending_row_and_appends_one(
        tmp_path, monkeypatch):
    """The scan writes the row once, before the reading exists. Attaching the
    reading afterwards is therefore necessarily a revise, not an edit — the
    same mechanism task 8.5 built for a vendor restating a figure — and that
    IS the real cost of deferring guidance out of the scan
    (docs/design/RANK_AFTER_GUIDANCE.ja.md, `hawkeye/scout/guidance_case.py`
    module docstring). This test pins that cost rather than hiding it."""
    result, store = _scan(tmp_path, monkeypatch)
    stock_id = store.stock_by_ticker("AMZN").id
    assert len(store.prints(stock_id)) == 1        # the pending row, alone

    _read_staged_guidance(store, {
        "guided": True, "period": "2026-Q3", "eps_low": -1.00, "eps_high": 0.0,
        "quote": ("third quarter results to range from a loss of $1.00 per "
                  "share to breakeven")})

    assert len(store.prints(stock_id)) == 2         # pending + the revision
    active = store.active_print(stock_id, "2026-Q2")
    assert active.guidance is not None


def test_a_refusal_is_recorded_by_name_on_the_row(tmp_path, monkeypatch):
    """An empty guidance leg has three causes and only one of them is ours. A
    row that stores the blank alone cannot tell them apart afterwards."""
    _, store = _scan(tmp_path, monkeypatch)

    _read_staged_guidance(store, {
        "guided": True, "period": "2026-Q3", "eps_low": 5.0, "eps_high": 6.0,
        "quote": "third quarter earnings of $5.00 to $6.00 per share"})

    row = store.active_print(store.stock_by_ticker("AMZN").id, "2026-Q2")
    assert row.guidance is None
    assert row.guidance_reason == "quote_not_in_source"


def test_the_reason_reaches_the_reading_of_the_quarter_after_rerank(
        tmp_path, monkeypatch):
    result, store = _scan(tmp_path, monkeypatch)

    _read_staged_guidance(store, {"guided": False})
    rerank_after_guidance(store, result, _config())

    guidance = result.passed[0].quality.guidance
    assert guidance.status is LegStatus.ABSENT
    assert "no_guidance_in_source" in guidance.flags


def test_a_scan_always_stages_rather_than_reading_inline(tmp_path, monkeypatch):
    """Nothing inside a scan process can reach an agent, so the sentence is
    always queued for `hawkeye guidance queue` / `submit` to close the loop —
    there is no code path left that reads it inline."""
    result, _ = _scan(tmp_path, monkeypatch)

    assert result.guidance.attempted == 0
    assert result.guidance.staged == 1
