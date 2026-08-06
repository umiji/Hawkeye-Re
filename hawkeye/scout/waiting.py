"""Holding a print open while its numbers are still on the way (pure logic).

The earnings feed publishes a company's new quarter some hours after the
release — measured 2026-08-05: every one of the 16 companies that reported
that morning still answered with its previous quarter. Two wrong answers are
available to a funnel that meets this:

- treat the missing number as "no beat" and drop the name. That discards
  precisely the prints the funnel exists to find, and it does so silently,
  because a company with no actual never reaches the surprise screen at all.
- treat the previous quarter's numbers as this quarter's. That compares one
  quarter's actual against another quarter's consensus — usually a large
  fabricated surprise, and the ranking puts the largest surprises first.

So the print is held instead: excluded from today's shortlist, re-read on the
next scan, and given up on 48 hours after the ANNOUNCEMENT. Not 48 hours after
midnight — a pre-market print and an after-close print on the same date are
eight hours apart, and the clock has to start where the news did.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Optional

from hawkeye.contracts.stocks import within_wait_window
from hawkeye.marketdata.whispers import EASTERN, WhispersRecord

__all__ = ["ActualStatus", "ActualReading", "announcement_moment",
           "read_actual", "within_wait_window"]

# US market hours, in the exchange's own timezone.
_OPEN = time(9, 30)
_CLOSE = time(16, 0)


class ActualStatus(str, Enum):
    ARRIVED = "arrived"
    MISSING = "missing"


@dataclass(frozen=True)
class ActualReading:
    """Whether this quarter's numbers are in hand, and if not, why not.

    The reason is the whole point: "the feed has no record of this company"
    and "the feed still holds last quarter" are both empty hands, but only the
    second one is worth waiting on, and a later review can only tell them
    apart if the distinction was written down at the time.
    """
    status: ActualStatus
    reason: str
    record: Optional[WhispersRecord] = None


def announcement_moment(report_date: date, hour_code: str,
                        record: Optional[WhispersRecord]) -> datetime:
    """When the news actually landed, in US Eastern.

    The feed's own timestamp wins when it belongs to THIS print. A record left
    over from the previous quarter carries a timestamp weeks old, and letting
    that start the clock would expire the wait before it began.
    """
    if record is not None and record.announced_at is not None \
            and record.covers(report_date):
        return record.announced_at
    at = _OPEN if (hour_code or "").lower() == "bmo" else _CLOSE
    return datetime.combine(report_date, at, tzinfo=EASTERN)


def read_actual(record: Optional[WhispersRecord],
                report_date: date) -> ActualReading:
    """What the feed's answer means for the print reported on `report_date`."""
    if record is None:
        return ActualReading(ActualStatus.MISSING, "no_record")
    stale = record.staleness_reason(report_date)
    if stale:
        return ActualReading(ActualStatus.MISSING, stale, record)
    if record.eps_actual is None:
        return ActualReading(ActualStatus.MISSING, "eps_actual_missing", record)
    return ActualReading(ActualStatus.ARRIVED, "", record)
