"""Waiting 48 hours for an actual that has not arrived yet.

The feed answers with the company's PREVIOUS quarter until it ingests the new
print — measured 2026-08-05: of 16 names that reported that morning, 16 came
back with their May quarter. Dropping those names would discard exactly the
prints the funnel exists to find, so they are held and re-read; holding them
forever would spend a request a day on data that is never coming, so the hold
is bounded at 48 hours from the ANNOUNCEMENT, not from midnight.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from hawkeye.ledger.store import Ledger
from hawkeye.marketdata.whispers import parse_details
from hawkeye.scout.waiting import (
    ActualStatus,
    announcement_moment,
    read_actual,
    within_wait_window,
)

ET = ZoneInfo("America/New_York")
FIXTURES = Path(__file__).parent / "fixtures" / "whispers"


def record(ticker: str):
    return parse_details(json.loads(
        (FIXTURES / f"{ticker}.json").read_text(encoding="utf-8",
                                                errors="replace")))


# -- when the clock starts -------------------------------------------------

def test_the_clock_starts_at_the_time_the_feed_itself_reports():
    assert announcement_moment(date(2026, 8, 4), "amc", record("ADM")) == \
        datetime(2026, 8, 4, 6, 0, tzinfo=ET)


def test_a_premarket_print_with_no_timestamp_starts_at_the_open():
    assert announcement_moment(date(2026, 8, 4), "bmo", None) == \
        datetime(2026, 8, 4, 9, 30, tzinfo=ET)


def test_an_after_close_print_with_no_timestamp_starts_at_the_close():
    assert announcement_moment(date(2026, 8, 4), "amc", None) == \
        datetime(2026, 8, 4, 16, 0, tzinfo=ET)


def test_an_unknown_release_time_starts_at_the_latest_plausible_moment():
    # The conservative direction: assuming the earliest would cut the wait
    # short for a company that reported after the close.
    assert announcement_moment(date(2026, 8, 4), "", None) == \
        datetime(2026, 8, 4, 16, 0, tzinfo=ET)


def test_a_stale_record_does_not_get_to_set_the_clock():
    # ACA's row is its April print; letting that timestamp start the clock
    # would expire the wait before it began.
    assert announcement_moment(date(2026, 8, 5), "bmo", record("ACA")) == \
        datetime(2026, 8, 5, 9, 30, tzinfo=ET)


# -- the window ------------------------------------------------------------

ANNOUNCED = datetime(2026, 8, 4, 16, 0, tzinfo=ET)


def test_a_print_is_still_worth_re_reading_after_forty_seven_hours():
    assert within_wait_window(ANNOUNCED,
                              ANNOUNCED + timedelta(hours=47, minutes=59), 48)


def test_the_boundary_itself_is_still_inside_the_window():
    assert within_wait_window(ANNOUNCED, ANNOUNCED + timedelta(hours=48), 48)


def test_a_minute_past_forty_eight_hours_is_outside_the_window():
    assert not within_wait_window(ANNOUNCED,
                                  ANNOUNCED + timedelta(hours=48, minutes=1),
                                  48)


# -- what the feed actually said -------------------------------------------

def test_a_matching_record_with_numbers_counts_as_arrived():
    reading = read_actual(record("ADM"), date(2026, 8, 4))
    assert reading.status is ActualStatus.ARRIVED
    assert reading.reason == ""


def test_a_previous_quarter_record_is_missing_and_says_so():
    reading = read_actual(record("ACA"), date(2026, 8, 5))
    assert reading.status is ActualStatus.MISSING
    assert reading.reason == "previous_quarter"


def test_no_record_at_all_is_missing_and_says_so():
    reading = read_actual(None, date(2026, 8, 5))
    assert reading.status is ActualStatus.MISSING
    assert reading.reason == "no_record"


def test_a_matching_record_without_an_eps_actual_is_still_missing():
    hollow = parse_details({"ticker": "X", "epsDate": "2026-08-04T16:00:00",
                            "q1Ref": "202606", "fY1Ref": "202612"})
    reading = read_actual(hollow, date(2026, 8, 4))
    assert reading.status is ActualStatus.MISSING
    assert reading.reason == "eps_actual_missing"


# -- the ledger side -------------------------------------------------------

def make_ledger(tmp_path) -> Ledger:
    return Ledger(str(tmp_path / "waits.db"))


def test_a_missing_actual_opens_a_wait_that_remembers_when_it_started(tmp_path):
    ledger = make_ledger(tmp_path)
    wait = ledger.note_missing_actual("ADM", date(2026, 8, 4), ANNOUNCED,
                                      "previous_quarter",
                                      at=ANNOUNCED + timedelta(hours=1))
    assert wait.announced_at == ANNOUNCED
    assert wait.attempts == 1
    assert wait.checks[-1]["reason"] == "previous_quarter"


def test_re_reading_the_same_print_never_restarts_the_clock(tmp_path):
    ledger = make_ledger(tmp_path)
    first = ledger.note_missing_actual("ADM", date(2026, 8, 4), ANNOUNCED,
                                       "no_record",
                                       at=ANNOUNCED + timedelta(hours=1))
    later = ledger.note_missing_actual("ADM", date(2026, 8, 4),
                                       ANNOUNCED + timedelta(hours=20),
                                       "previous_quarter",
                                       at=ANNOUNCED + timedelta(hours=20))
    assert later.first_seen_at == first.first_seen_at
    assert later.announced_at == ANNOUNCED
    assert later.attempts == 2


def test_every_failed_read_leaves_its_own_reason_behind(tmp_path):
    ledger = make_ledger(tmp_path)
    for hours, reason in ((1, "no_record"), (5, "feed_unavailable"),
                          (9, "previous_quarter")):
        ledger.note_missing_actual("ADM", date(2026, 8, 4), ANNOUNCED, reason,
                                   at=ANNOUNCED + timedelta(hours=hours))
    wait = ledger.actual_wait("ADM", date(2026, 8, 4))
    assert [c["reason"] for c in wait.checks] == \
        ["no_record", "feed_unavailable", "previous_quarter"]


def test_a_wait_inside_the_window_is_offered_to_the_next_scan(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.note_missing_actual("ADM", date(2026, 8, 4), ANNOUNCED,
                               "no_record", at=ANNOUNCED)
    open_now = ledger.open_actual_waits(ANNOUNCED + timedelta(hours=47), 48)
    assert [(w.ticker, w.report_date) for w in open_now] == \
        [("ADM", date(2026, 8, 4))]


def test_a_wait_past_the_window_is_closed_with_its_reason(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.note_missing_actual("ADM", date(2026, 8, 4), ANNOUNCED,
                               "previous_quarter", at=ANNOUNCED)
    expired = ledger.expire_actual_waits(ANNOUNCED + timedelta(hours=49), 48)
    assert [w.ticker for w in expired] == ["ADM"]
    assert expired[0].resolution == "expired_48h"
    assert expired[0].checks[-1]["reason"] == "previous_quarter"
    assert ledger.open_actual_waits(ANNOUNCED + timedelta(hours=49), 48) == []


def test_expiring_twice_reports_the_wait_once(tmp_path):
    # The tally that decides whether the screen gets revised counts these.
    ledger = make_ledger(tmp_path)
    ledger.note_missing_actual("ADM", date(2026, 8, 4), ANNOUNCED,
                               "no_record", at=ANNOUNCED)
    ledger.expire_actual_waits(ANNOUNCED + timedelta(hours=49), 48)
    assert ledger.expire_actual_waits(ANNOUNCED + timedelta(hours=72), 48) == []


def test_an_arriving_actual_ends_the_wait(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.note_missing_actual("ADM", date(2026, 8, 4), ANNOUNCED,
                               "no_record", at=ANNOUNCED)
    ledger.resolve_actual_wait("ADM", date(2026, 8, 4), "actual_arrived",
                               at=ANNOUNCED + timedelta(hours=20))
    assert ledger.open_actual_waits(ANNOUNCED + timedelta(hours=21), 48) == []
    assert ledger.actual_wait("ADM", date(2026, 8, 4)).resolution == \
        "actual_arrived"


def test_a_closed_wait_is_not_reopened_by_a_later_sighting(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.note_missing_actual("ADM", date(2026, 8, 4), ANNOUNCED,
                               "no_record", at=ANNOUNCED)
    ledger.expire_actual_waits(ANNOUNCED + timedelta(hours=49), 48)
    ledger.note_missing_actual("ADM", date(2026, 8, 4), ANNOUNCED,
                               "no_record", at=ANNOUNCED + timedelta(hours=50))
    assert ledger.open_actual_waits(ANNOUNCED + timedelta(hours=50), 48) == []
