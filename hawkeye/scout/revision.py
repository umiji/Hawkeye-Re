"""Reported figures that move after the print (task 8.5).

ADEA reported 2026-Q2 on 2026-08-05 and its EPS actual went from $0.34 to
$0.42 the following day, revenue unchanged. That is a 24% move in the
numerator of a surprise ratio, on a name that was already inside the
shortlist — enough to reorder the run, and in the general case enough to turn
a beat into a miss.

The scan already re-reads a print on every overlapping window, so the second
reading costs nothing extra; what was missing was noticing that it differed.
`_record_print` used to skip a repeat outright, which is correct for a repeat
and wrong for a correction.

Detection is deliberately narrow. Four things are NOT revisions:

- a repeat carrying identical figures (the normal case, every scan);
- a difference below what the vendor itself publishes — EPS to the cent,
  revenue to a hundredth of a percent — which is float noise;
- absent becoming present, which is the hold ending (task 6), not a change of
  mind about a number;
- anything after the watch window closes, because a figure restated months
  later would reopen a quarter the ledger has finished with, on a scan nobody
  ran for that purpose.

Present becoming ABSENT is a revision, and a loud one: the vendor withdrew a
number the ranking used, and keeping the old value would be the system
preferring its own memory to its source.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional

from hawkeye.contracts.stocks import EarningsPrint, PrintSource

# What the vendor itself publishes. A difference smaller than this cannot be a
# restatement — it is the last bits of a float — and reporting it would train
# the reader to skip the message that matters.
_EPS_STEP = 0.005                # published to the cent
_REVENUE_RELATIVE_STEP = 0.0001  # a hundredth of a percent


@dataclass(frozen=True)
class Revision:
    """One figure that moved, named in the terms the reader needs: which
    company, which quarter, which figure, and both values."""
    ticker: str
    fiscal_quarter: str
    field: str
    before: Optional[float]
    after: Optional[float]

    @property
    def change_pct(self) -> Optional[float]:
        if not self.before or self.after is None:
            return None
        return (self.after - self.before) / abs(self.before) * 100.0


def _moved(field: str, before: Optional[float],
           after: Optional[float]) -> bool:
    if before is None:
        return False                      # absent -> present is the hold ending
    if after is None:
        return True                       # withdrawn: always worth seeing
    if field == "eps_actual":
        return abs(after - before) >= _EPS_STEP
    scale = max(abs(before), abs(after))
    return scale > 0 and abs(after - before) / scale >= _REVENUE_RELATIVE_STEP


def _eps(row: EarningsPrint) -> Optional[float]:
    """The EPS figure this row actually stands on.

    A print row keeps each vendor's actual in its own field: the chosen
    vendor's in `eps_actual`, the calendar's in `eps_actual_rows`. For a
    calendar-backed print `eps_actual` is therefore empty, and comparing that
    field alone would report every calendar print as "no figure" and never see
    a correction — which is exactly what it did until this was fixed.

    Same rule as the reading in `hawkeye/scout/quality.py`: whoever `source`
    names supplies the number, and the calendar's contradictory rows resolve
    to nothing rather than to a guess.
    """
    if row.source is PrintSource.WHISPERS:
        return row.eps_actual
    return row.eps_actual_rows_usable


def _announced(row: EarningsPrint) -> datetime:
    return row.reported_at or datetime.combine(
        row.report_date, time.max, tzinfo=timezone.utc)


def detect_revisions(existing: EarningsPrint, incoming: EarningsPrint,
                     config, now: Optional[datetime] = None) -> list[Revision]:
    """Which of `existing`'s figures `incoming` restates, or an empty list.

    `now` is passed rather than read so the window is testable and so one scan
    judges every print against a single moment.
    """
    moment = now or datetime.now(timezone.utc)
    elapsed = (moment - _announced(existing)).total_seconds() / 3600.0
    if elapsed > config.actual_revision_watch_hours:
        return []
    return [Revision(ticker=existing.ticker,
                     fiscal_quarter=existing.fiscal_quarter,
                     field=field, before=before, after=after)
            for field, before, after in (
                ("eps_actual", _eps(existing), _eps(incoming)),
                ("revenue_actual", existing.revenue_actual,
                 incoming.revenue_actual))
            if _moved(field, before, after)]


def apply_revision(store, incoming: EarningsPrint) -> str:
    """Record the corrected figures as a new active row, retiring the old one.

    Thin on purpose: `StockStore.revise_print` already does both halves in one
    transaction, and the reason a separate name exists here is that the caller
    reads better for it — `_record_print` records, this revises, and the two
    are never the same decision.
    """
    return store.revise_print(incoming)
