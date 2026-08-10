"""Prints whose own numbers have not arrived, held rather than ranked.

The earnings feed publishes a company's new quarter roughly a day late — 16 of
16 names that reported on the morning of 2026-08-05 still answered with their
May quarter. Ranking such a name on the calendar's figures instead would look
harmless and is not: the print row gets written, the dedup refuses the print on
every later scan, and the feed's own reading is never taken. The name is
permanently judged on the yardstick the whole migration exists to replace.

So the print is HELD: not enriched, not gated, not ranked, no print row
written, and recorded as `actual_pending` so the next scan reads it again.
`earnings_actual_wait_hours` bounds that at 48 hours, after which it is given
up on as `actual_timeout` — a fact about our data, recorded as one, which the
drop review counts.

Two properties are load-bearing and easy to break:

1. **The clock starts at the calendar's report date**, never at the feed's
   announcement time. While a print is held, the feed is answering with the
   PREVIOUS quarter, so its timestamp is months old and the wait would be over
   before it began.
2. **The dedup must not refuse a held print.** `screened_candidates` is what
   the dedup reads, so a pending row that counted as "already seen" would
   close the very door the hold exists to keep open.
"""
from __future__ import annotations

from dataclasses import replace as dc_replace
from datetime import date, timedelta

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import ScreenedCandidateStage
from hawkeye.ledger.store import Ledger
from hawkeye.marketdata.base import StaticProvider
from hawkeye.marketdata.whispers import WhispersUnavailable
from hawkeye.scout.earnings import EarningsEvent
from hawkeye.scout.scout import build_screened_candidates, run_scout
from hawkeye.scout.waiting import held_reason, wait_expired
from tests.conftest import FakeWhispers, make_bars, make_whispers

CONFIG = HawkeyeConfig()


class FakeCalendar:
    def __init__(self, entries):
        self.entries = entries

    def earnings_calendar(self, start, end):
        return self.entries


def a_run_day(today: date) -> date:
    """A day to pretend the scan runs on, such that "one trading day ago" is
    ALSO within the 48-hour hold window.

    The two clocks in play disagree at a weekend and the tests sat on the
    disagreement. The freshness gate counts TRADING days, so an event has to be
    dated on a weekday; the hold counts CALENDAR hours from the report date.
    Run on a Monday, one trading day back is Friday — 72 hours — and every
    "still waiting" case times out instead. Found on Monday 2026-08-10, the day
    after the weekend fix that introduced `a_business_day_ago`.

    Stepping the pretend run day back to Friday makes both clocks agree on
    every real weekday, so these tests no longer pass or fail by what day it
    happens to be.

    NOTE this is a test fixture, not a workaround for a production bug that has
    been fixed: a print released after Friday's close and first scanned on
    Monday really is 72 hours old, and really is given up on before the feed —
    which publishes about a day late — has had a business day to answer.
    """
    return today - timedelta(days={0: 3, 5: 1, 6: 2}.get(today.weekday(), 0))


def a_business_day_ago(days: int, today: date) -> date:
    """`days` trading days before `today`.

    NOT `today - timedelta(days)`. The entry gate that measures how fresh a
    catalyst is counts TRADING days from the price series, so an event dated
    on a Saturday reads as older than it is — and these tests then pass Monday
    to Friday and fail at the weekend. Found on a Sunday, 2026-08-09.
    """
    day = today
    stepped = 0
    while stepped < days:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            stepped += 1
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _event(**kw) -> EarningsEvent:
    base = dict(ticker="AAA", day=date(2026, 8, 5), eps_actual=1.20,
                eps_estimate=1.00, revenue_actual=1.02e9,
                revenue_estimate=1.0e9)
    base.update(kw)
    return EarningsEvent(**base)


# --- which prints are held ------------------------------------------------

def test_the_feed_still_on_last_quarter_is_held():
    assert held_reason(_event(numbers_reason="whispers_previous_quarter")) \
        == "whispers_previous_quarter"


def test_a_connection_failure_is_held_because_it_says_nothing_about_the_company():
    """Nothing came back at all, so nothing was learned about the company.
    Ranking the name on the calendar instead would spend the print's one
    chance at the feed on a network blip."""
    assert held_reason(_event(numbers_reason="whispers_unreachable")) \
        == "whispers_unreachable"


def test_a_server_error_the_ticker_reproduces_is_not_held():
    """Corrects a reading from 2026-08-07. These 500s looked sporadic and are
    not: the same tickers return the same error page every time. Holding them
    buys the identical refusal on every scan for 48 hours and then times out,
    so the print falls back to the calendar like any other decline the feed
    cannot recover from."""
    assert held_reason(_event(numbers_reason="whispers_server_error")) == ""


def test_a_missing_consensus_is_not_held_because_waiting_cannot_fix_it():
    """Consensus does not appear after the fact. The whole print moves to the
    calendar and is ranked there, which is the documented fallback."""
    assert held_reason(_event(numbers_reason="whispers_revenue_incomplete")) == ""
    assert held_reason(_event(numbers_reason="whispers_eps_incomplete")) == ""


def test_a_company_the_feed_has_no_record_for_is_not_held():
    assert held_reason(_event(numbers_reason="whispers_no_record")) == ""


def test_a_later_print_is_not_held_because_waiting_will_not_help():
    """The feed answering with a NEWER print means the calendar's date was
    wrong, not that the feed is behind."""
    assert held_reason(_event(numbers_reason="whispers_later_print")) == ""


def test_a_print_the_feed_answered_for_is_never_held():
    assert held_reason(_event(numbers_source="whispers")) == ""


def test_the_calendar_contradicting_itself_holds_when_it_is_the_source():
    """Its actual is unusable, so there is nothing to rank on — but the feed
    may still catch up, and giving up now would never find out."""
    held = _event(all_eps_actuals=(1.88, 1.97))
    assert held_reason(held) == "calendar_actual_conflict"


def test_contradictory_calendar_rows_do_not_hold_a_print_the_feed_answered():
    covered = _event(numbers_source="whispers", all_eps_actuals=(1.88, 1.97))
    assert held_reason(covered) == ""


# --- the clock ------------------------------------------------------------

def test_the_wait_runs_from_the_calendars_report_date():
    """Not from the feed's announcement time: while a print is held the feed
    is answering with the previous quarter, so that timestamp is months old
    and every held print would time out on its first look."""
    reported = date(2026, 8, 5)
    assert not wait_expired(reported, reported, hours=48)
    assert not wait_expired(reported, reported + timedelta(days=2), hours=48)
    assert wait_expired(reported, reported + timedelta(days=3), hours=48)


def test_the_window_is_one_config_number():
    reported = date(2026, 8, 5)
    assert wait_expired(reported, reported + timedelta(days=2), hours=24)
    assert not wait_expired(reported, reported + timedelta(days=2), hours=72)


# --- the funnel -----------------------------------------------------------

def _entries(day: date) -> list[dict]:
    return [{"symbol": "HELD", "date": day.isoformat(), "year": 2026,
             "quarter": 2, "epsActual": 1.30, "epsEstimate": 1.00,
             "revenueActual": 1.05e9, "revenueEstimate": 1.0e9},
            {"symbol": "READY", "date": day.isoformat(), "year": 2026,
             "quarter": 2, "epsActual": 1.20, "epsEstimate": 1.00,
             "revenueActual": 1.05e9, "revenueEstimate": 1.0e9}]


def _provider() -> StaticProvider:
    return StaticProvider(bars=make_bars(30, start_price=40.0,
                                         volume=2_000_000),
                          profile_data={"market_cap": 5e9})


def _feed(day: date) -> FakeWhispers:
    return FakeWhispers({
        # Still answering with the quarter before this one.
        "HELD": make_whispers("HELD", announced=day - timedelta(days=90)),
        "READY": make_whispers("READY", announced=day, eps_actual=1.21,
                               eps_consensus=1.00)})


def test_a_held_print_is_not_enriched_ranked_or_recorded(tmp_path):
    from hawkeye.ledger.stocks import StockStore

    today = a_run_day(date.today())
    day = a_business_day_ago(1, today)
    store = StockStore(str(tmp_path / "hawkeye.db"))

    result = run_scout(FakeCalendar(_entries(day)), _provider(), CONFIG,
                       today=today, numbers_source=_feed(day),
                       stock_store=store)

    assert [c.ticker for c in result.held] == ["HELD"]
    assert [c.ticker for c in result.passed] == ["READY"]
    assert result.enriched == 1                     # the held name cost nothing
    # No print row: a row would make the quarter "already recorded", and the
    # next scan would skip it forever.
    stock = store.stock_by_ticker("HELD")
    assert stock is None or store.active_print(stock.id, "2026-Q2") is None


def test_a_held_print_is_recorded_as_pending_not_as_a_rejection(tmp_path):
    today = a_run_day(date.today())
    day = a_business_day_ago(1, today)

    result = run_scout(FakeCalendar(_entries(day)), _provider(), CONFIG,
                       today=today, numbers_source=_feed(day))
    rows = build_screened_candidates(result, scan_id=1)

    held = next(r for r in rows if r.ticker == "HELD")
    assert held.stage is ScreenedCandidateStage.ACTUAL_PENDING
    assert "whispers_previous_quarter" in held.reject_reason
    # What the screen saw is still recorded — the drop review needs it even
    # for a name it will not count.
    assert held.eps_surprise_pct == 30.0


def test_a_held_print_past_the_window_is_given_up_on():
    today = a_run_day(date.today())
    day = a_business_day_ago(5, today)          # well past 48 hours

    result = run_scout(FakeCalendar(_entries(day)), _provider(), CONFIG,
                       today=today, numbers_source=_feed(day))
    rows = build_screened_candidates(result, scan_id=2)

    held = next(r for r in rows if r.ticker == "HELD")
    assert held.stage is ScreenedCandidateStage.ACTUAL_TIMEOUT


def test_an_unreachable_feed_holds_the_print_rather_than_ranking_it():
    today = a_run_day(date.today())
    day = a_business_day_ago(1, today)
    feed = FakeWhispers({"HELD": WhispersUnavailable("boom"),
                         "READY": make_whispers("READY", announced=day)})

    result = run_scout(FakeCalendar(_entries(day)), _provider(), CONFIG,
                       today=today, numbers_source=feed)

    assert [c.ticker for c in result.held] == ["HELD"]


# --- the dedup exemption --------------------------------------------------

def test_a_pending_print_is_read_again_on_the_next_scan(tmp_path):
    """`screened_candidates` is what the dedup reads, so a pending row that
    counted as "already seen" would close the door the hold exists to keep
    open — and the print would never be read again."""
    ledger = Ledger(str(tmp_path / "hawkeye.db"))
    today = a_run_day(date.today())
    day = a_business_day_ago(1, today)

    scan_id = ledger.record_scan(params={}, scanned=2, screened=2, enriched=1,
                                 gate_passed=1, tickers=["READY"])
    result = run_scout(FakeCalendar(_entries(day)), _provider(), CONFIG,
                       today=today, numbers_source=_feed(day))
    # Nothing forwarded to the tribunal this run, so READY is recorded at the
    # ranking cutoff — a real decision, and one the dedup must honour.
    ledger.record_screened_candidates(
        scan_id, build_screened_candidates(result, scan_id, 0))

    seen = ledger.seen_events()
    assert ("HELD", day) not in seen          # still open
    assert ("READY", day) in seen             # settled, never re-read


def test_a_print_given_up_on_is_never_read_again(tmp_path):
    ledger = Ledger(str(tmp_path / "hawkeye.db"))
    today = a_run_day(date.today())
    day = a_business_day_ago(5, today)

    scan_id = ledger.record_scan(params={}, scanned=2, screened=2, enriched=1,
                                 gate_passed=1, tickers=[])
    result = run_scout(FakeCalendar(_entries(day)), _provider(), CONFIG,
                       today=today, numbers_source=_feed(day))
    ledger.record_screened_candidates(
        scan_id, build_screened_candidates(result, scan_id, 0))

    assert ("HELD", day) in ledger.seen_events()


def test_the_hold_survives_being_switched_off_by_config():
    """A doctrine change is a config diff (invariant 7). At zero hours every
    held print is given up on immediately, which is the behaviour before this
    feature existed — minus the silent ranking on the calendar's numbers."""
    today = a_run_day(date.today())
    day = a_business_day_ago(1, today)
    off = dc_replace(CONFIG, earnings_actual_wait_hours=0)

    result = run_scout(FakeCalendar(_entries(day)), _provider(), off,
                       today=today, numbers_source=_feed(day))
    rows = build_screened_candidates(result, scan_id=3)

    held = next(r for r in rows if r.ticker == "HELD")
    assert held.stage is ScreenedCandidateStage.ACTUAL_TIMEOUT
