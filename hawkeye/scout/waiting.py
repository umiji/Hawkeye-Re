"""Prints whose own numbers have not arrived yet, and how long we wait.

The earnings feed publishes a company's new quarter roughly a day late —
measured 2026-08-05: every one of the 16 companies that reported that morning
still answered with its previous quarter. Two wrong answers are available to a
funnel that meets this:

- treat the missing number as "no beat" and drop the name. That discards
  precisely the prints the funnel exists to find, and silently.
- rank it on the CALENDAR's figures instead. This looks harmless and is not:
  the print row gets written, the dedup refuses the print on every later scan,
  and the feed's own reading is never taken — so the name is permanently
  judged on the yardstick the migration exists to replace.

So the print is HELD: not enriched, not gated, not ranked, and given no print
row. It is recorded in the dropped-candidate table as `actual_pending`, one row
per scan, so the whole life of one print reads in order:

    scan 12  ADM 2026-08-04  actual_pending   the feed answered with Q1
    scan 13  ADM 2026-08-04  actual_pending   the feed answered with Q1
    scan 14  ADM 2026-08-04  ranking_cutoff   numbers arrived, ranked 7th

There is deliberately no separate table. The earlier design had one
(`earnings_actual_waits` — defined, and never written to by anything) on the
reasoning that an append-only drop log cannot hold state. It can: a row per
look IS the state, and it is more auditable than a JSON array accumulating
inside a single row. Splitting them would also put the END of a print's life
(`actual_timeout`, always planned for the drop log) in a different table from
its middle.

Two properties are load-bearing:

1. **The clock starts at the calendar's report date.** Never at the feed's
   announcement time — while a print is held the feed is answering with the
   PREVIOUS quarter, so that timestamp is months old and every held print
   would time out on its first look.
2. **The dedup must not refuse a held print** (`Ledger.seen_events`), or the
   hold closes the very door it exists to keep open.
"""
from __future__ import annotations

from datetime import date, timedelta

from hawkeye.scout.earnings import EarningsEvent

__all__ = ["held_reason", "wait_expired"]

# Why the feed declined, when waiting is the right answer. Everything else it
# can say is permanent for this print: a missing consensus does not appear
# after the fact, a company the feed has no record for will not gain one by
# tomorrow, and a NEWER print means the calendar's date was wrong rather than
# the feed being behind. Those fall back to the calendar and are ranked there.
_WORTH_WAITING_FOR = frozenset({
    # The feed has not ingested this print yet. The normal case, and the
    # reason this whole mechanism exists.
    "whispers_previous_quarter",
    # Nothing came back at all, so nothing was learned about the company.
    # NOT the same as a server error: `whispers_server_error` reproduces on
    # every attempt for the same ticker (INOD/UMAC/GAIN, measured 2026-08-08),
    # so holding those buys the identical refusal every scan for 48 hours and
    # then times out anyway. They fall back to the calendar.
    "whispers_unreachable",
    # The feed answered without an announcement time, so whether it is even
    # the same print cannot be established. Fail closed (invariant 6).
    "whispers_announcement_time_missing",
})


def held_reason(event: EarningsEvent) -> str:
    """Why this print cannot be ranked yet — "" when it can.

    Named rather than boolean because the reason is what the drop record
    carries, and "the feed is behind" and "the calendar contradicts itself"
    read as different findings when the held rows are reviewed later.
    """
    if event.numbers_reason in _WORTH_WAITING_FOR:
        return event.numbers_reason
    # The calendar returning two different actuals for one print (AMZN: 1.88
    # and 1.97) makes its actual unusable, so there is nothing to rank on when
    # the calendar is the source. Picking the more plausible row is the
    # judgment this system exists to remove — and the feed may still catch up.
    if event.numbers_source != "whispers" and len(
            {round(v, 6) for v in event.all_eps_actuals}) > 1:
        return "calendar_actual_conflict"
    return ""


def wait_expired(report_date: date, today: date, hours: int) -> bool:
    """Whether a print held since `report_date` should be given up on.

    Whole days, because the earnings calendar gives a date and not a time. The
    window covers the report date plus `hours` worth of days INCLUSIVE, so 48
    hours means the print is still read on the report date and the two days
    after it — erring toward one more look, which costs a request rather than
    a candidate.
    """
    return today > report_date + timedelta(days=hours // 24)
