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
    """A transient HTTP 500 is a fact about the site. Ranking the name on the
    calendar instead would spend the print's one chance at the feed on a
    network blip — measured at 1 in 50 to 5 in 50 per run."""
    assert held_reason(_event(numbers_reason="whispers_unreachable")) \
        == "whispers_unreachable"


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

    today = date.today()
    day = today - timedelta(days=1)
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
    today = date.today()
    day = today - timedelta(days=1)

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
    today = date.today()
    day = today - timedelta(days=5)          # well past 48 hours

    result = run_scout(FakeCalendar(_entries(day)), _provider(), CONFIG,
                       today=today, numbers_source=_feed(day))
    rows = build_screened_candidates(result, scan_id=2)

    held = next(r for r in rows if r.ticker == "HELD")
    assert held.stage is ScreenedCandidateStage.ACTUAL_TIMEOUT


def test_an_unreachable_feed_holds_the_print_rather_than_ranking_it():
    today = date.today()
    day = today - timedelta(days=1)
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
    today = date.today()
    day = today - timedelta(days=1)

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
    today = date.today()
    day = today - timedelta(days=5)

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
    today = date.today()
    day = today - timedelta(days=1)
    off = dc_replace(CONFIG, earnings_actual_wait_hours=0)

    result = run_scout(FakeCalendar(_entries(day)), _provider(), off,
                       today=today, numbers_source=_feed(day))
    rows = build_screened_candidates(result, scan_id=3)

    held = next(r for r in rows if r.ticker == "HELD")
    assert held.stage is ScreenedCandidateStage.ACTUAL_TIMEOUT
