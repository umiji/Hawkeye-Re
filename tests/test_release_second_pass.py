"""The second pass of the release-read workflow: a print held open until a
document arrives (docs/design/MASTER_OVERVIEW.ja.md §5.3 実装順序(c)).

The read happens outside this process, so the funnel names the prints it
wants and picks the documents up on a LATER run. That only works if the
later run can still see those prints, and by default it cannot: scan windows
move on, and a name already recorded as a dropped candidate is deduplicated
out of every subsequent run. Shipped 2026-08-03, the feature therefore
prepared documents nobody ever read.

The exemption is deliberately narrow — one print, named by the funnel itself,
for a bounded time — because the dedup it suspends exists for two real
reasons: windows overlap by design, and a candidate evaluated twice would be
counted twice in the drop statistics that authorize screen revisions.
"""
from __future__ import annotations

from datetime import date, timedelta

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.stocks import PrintDepth
from hawkeye.ledger.stocks import StockStore
from hawkeye.ledger.store import Ledger
from hawkeye.marketdata.base import StaticProvider
from hawkeye.marketdata.yahoo_earnings import VerifiedEarnings
from hawkeye.scout.quality import LegStatus
from hawkeye.scout.release import parse_release_key
from hawkeye.scout.scout import ScanWindow, build_screened_candidates, run_scout
from tests.conftest import make_bars
from tests.test_release_read import (
    FakeDirectory,
    FakeFacts,
    FakeReleases,
    _extraction,
)


def _config() -> HawkeyeConfig:
    return HawkeyeConfig()


def _provider() -> StaticProvider:
    return StaticProvider(bars=make_bars(30, start_price=40.0,
                                         volume=2_000_000),
                          profile_data={"market_cap": 5e9})


class RangedCalendar:
    """A calendar that actually honours the requested range.

    The other scout tests use a stub that returns everything regardless of
    dates; here the range is the whole point, because the print this test
    follows falls OUT of the second run's window.
    """

    def __init__(self, entries: list[dict]):
        self.entries = entries
        self.ranges: list[tuple[date, date]] = []

    def earnings_calendar(self, start: date, end: date) -> list[dict]:
        self.ranges.append((start, end))
        return [row for row in self.entries
                if start <= date.fromisoformat(row["date"]) <= end]


class FakeNumbers:
    def __init__(self, found: dict):
        self.found = found

    def verified_earnings(self, ticker, day):
        return self.found.get(ticker)


def _disputed_entry(ticker: str, day: date) -> dict:
    """The calendar's (street) reading; Yahoo's filed one differs by $0.11 —
    the AAPL shape, which is 21% of prints."""
    return {"symbol": ticker, "date": day.isoformat(), "year": 2026,
            "quarter": 3, "epsActual": 1.91, "epsEstimate": 1.80,
            "revenueActual": 1.05e9, "revenueEstimate": 1.0e9}


def _yahoo(ticker: str, day: date) -> VerifiedEarnings:
    return VerifiedEarnings(ticker=ticker, report_date=day, eps_actual=2.02,
                            eps_estimate=1.89, surprise_pct=6.88)


def _run(calendar, releases, ledger, store, window: ScanWindow, today: date,
         held_open=None, facts=None, numbers="disputed"):
    """One scout run against the fakes, with the second source's reading of
    AAPL either disagreeing with the calendar (the default, and the case this
    whole workflow exists for) or absent."""
    source = (FakeNumbers({"AAPL": _yahoo("AAPL", window.end)})
              if numbers == "disputed" else None)
    return run_scout(
        calendar, _provider(), _config(), today=today, window=window,
        already_seen=ledger.seen_events(), held_open=held_open,
        numbers_source=source,
        stock_store=store, directory=FakeDirectory({"AAPL": "0000320193"}),
        facts=facts if facts is not None else FakeFacts({"0000320193": 2.02}),
        release_reader=releases)


# --- the workflow, end to end ----------------------------------------------

def test_a_print_awaiting_a_document_is_evaluated_again_on_the_next_run(tmp_path):
    """The whole point of naming a print: the document arrives between runs,
    and the run after that has to pick it up. Before this fix the name was
    deduplicated out and the file sat unread forever."""
    db = str(tmp_path / "hawkeye.db")
    ledger, store = Ledger(db), StockStore(db)
    day = date(2026, 7, 31)
    calendar = RangedCalendar([_disputed_entry("AAPL", day)])
    releases = FakeReleases({})                     # nothing to read yet

    first = _run(calendar, releases, ledger, store,
                 ScanWindow(start=day, end=day), today=day + timedelta(days=1))
    assert first.release_wanted == [f"AAPL_{day.isoformat()}"]
    scan_id = ledger.record_scan({}, 1, 1, 1, len(first.passed), ["AAPL"])
    ledger.record_screened_candidates(
        scan_id, build_screened_candidates(first, scan_id))
    opened = ledger.request_release_reads(
        [parse_release_key(k) for k in first.release_wanted])
    assert opened == 1

    # …a human or an agent reads the 8-K and drops the extraction in.
    releases.extractions["AAPL"] = _extraction(one_off_per_share=0.11)

    later = day + timedelta(days=4)
    second = _run(calendar, releases, ledger, store,
                  ScanWindow(start=later, end=later), today=later + timedelta(days=1),
                  held_open=ledger.open_release_requests(later, max_age_days=14))

    assert second.reopened == [("AAPL", day)]
    assert second.release_settled == [f"AAPL_{day.isoformat()}"]
    assert second.passed[0].quality.eps.status is LegStatus.BEAT
    row = store.latest_print(store.stock_by_ticker("AAPL").id, "2026-Q3")
    assert row.depth is PrintDepth.RELEASE_READ
    assert row.eps_release == 1.91


def test_a_settled_print_is_not_held_open_a_third_time(tmp_path):
    """Once the document has been read the exemption ends, or the name would
    be re-enriched on every run for the rest of its window."""
    db = str(tmp_path / "hawkeye.db")
    ledger = Ledger(db)
    day = date(2026, 7, 31)
    ledger.request_release_reads([("AAPL", day)])

    ledger.resolve_release_reads([("AAPL", day)], "read")

    assert ledger.open_release_requests(day + timedelta(days=1),
                                        max_age_days=14) == set()


def test_a_document_that_never_arrives_stops_being_waited_for(tmp_path):
    """Bounded by the entry gate that would refuse the trade anyway: past
    `max_event_age_days` trading days the catalyst is too old to act on, so
    holding the print open longer buys nothing and costs a fetch every run."""
    db = str(tmp_path / "hawkeye.db")
    ledger = Ledger(db)
    day = date(2026, 7, 31)
    ledger.request_release_reads([("AAPL", day)])

    assert ledger.open_release_requests(day + timedelta(days=13),
                                        max_age_days=14) == {("AAPL", day)}
    assert ledger.open_release_requests(day + timedelta(days=15),
                                        max_age_days=14) == set()


def test_asking_twice_keeps_the_original_request(tmp_path):
    """The age bound is counted from the first ask. Re-requesting on every
    run would keep resetting it, which is how a bounded wait becomes an
    unbounded one."""
    db = str(tmp_path / "hawkeye.db")
    ledger = Ledger(db)
    day = date(2026, 7, 31)

    assert ledger.request_release_reads([("AAPL", day)]) == 1
    assert ledger.request_release_reads([("AAPL", day)]) == 0
    assert ledger.open_release_requests(day + timedelta(days=1),
                                        max_age_days=14) == {("AAPL", day)}


# --- what the exemption must NOT break --------------------------------------

def test_a_reopened_candidate_is_not_recorded_as_a_drop_twice(tmp_path):
    """The drop statistics decide when the screen gets revised, and 20
    samples of one cause is the bar. A print counted twice because we went
    back for a document would inflate exactly that tally."""
    db = str(tmp_path / "hawkeye.db")
    ledger, store = Ledger(db), StockStore(db)
    day = date(2026, 7, 31)
    calendar = RangedCalendar([_disputed_entry("AAPL", day)])
    releases = FakeReleases({})

    first = _run(calendar, releases, ledger, store,
                 ScanWindow(start=day, end=day), today=day + timedelta(days=1))
    scan_id = ledger.record_scan({}, 1, 1, 1, len(first.passed), ["AAPL"])
    ledger.record_screened_candidates(
        scan_id, build_screened_candidates(first, scan_id))
    ledger.request_release_reads(
        [parse_release_key(k) for k in first.release_wanted])
    assert len(ledger.screened_candidates()) == 1

    later = day + timedelta(days=4)
    second = _run(calendar, releases, ledger, store,
                  ScanWindow(start=later, end=later),
                  today=later + timedelta(days=1),
                  held_open=ledger.open_release_requests(later, max_age_days=14))

    assert second.reopened == [("AAPL", day)]
    assert build_screened_candidates(second, scan_id + 1) == []


def test_an_ordinary_repeat_is_still_deduplicated(tmp_path):
    """The exemption is for named prints only. Everything else keeps the
    behaviour that stops an overlapping window re-evaluating the same
    earnings event."""
    db = str(tmp_path / "hawkeye.db")
    ledger, store = Ledger(db), StockStore(db)
    day = date(2026, 7, 31)
    calendar = RangedCalendar([{"symbol": "AAPL", "date": day.isoformat(),
                                "year": 2026, "quarter": 3, "epsActual": 1.20,
                                "epsEstimate": 1.00, "revenueActual": 1.05e9,
                                "revenueEstimate": 1.0e9}])
    releases = FakeReleases({})

    first = _run(calendar, releases, ledger, store,
                 ScanWindow(start=day, end=day), today=day + timedelta(days=1),
                 facts=FakeFacts({"0000320193": 1.20}), numbers="absent")
    scan_id = ledger.record_scan({}, 1, 1, 1, len(first.passed), ["AAPL"])
    ledger.record_screened_candidates(
        scan_id, build_screened_candidates(first, scan_id))
    assert first.release_wanted == []

    second = _run(calendar, releases, ledger, store,
                  ScanWindow(start=day, end=day), today=day + timedelta(days=1),
                  held_open=ledger.open_release_requests(day, max_age_days=14),
                  facts=FakeFacts({"0000320193": 1.20}), numbers="absent")

    assert second.duplicates == 1
    assert second.reopened == []
    assert second.passed == []


def test_a_held_open_print_outside_the_window_costs_one_extra_lookup(tmp_path):
    """The print is no longer in the scan window at all, so the exemption
    alone would not find it — the calendar has to be asked for it by name.
    One extra call per run, not one per print."""
    db = str(tmp_path / "hawkeye.db")
    ledger, store = Ledger(db), StockStore(db)
    day = date(2026, 7, 31)
    calendar = RangedCalendar([_disputed_entry("AAPL", day)])
    releases = FakeReleases({"AAPL": _extraction(one_off_per_share=0.11)})
    ledger.request_release_reads([("AAPL", day)])

    later = day + timedelta(days=4)
    _run(calendar, releases, ledger, store,
         ScanWindow(start=later, end=later), today=later + timedelta(days=1),
         held_open=ledger.open_release_requests(later, max_age_days=14))

    assert calendar.ranges == [(later, later), (day, day)]
