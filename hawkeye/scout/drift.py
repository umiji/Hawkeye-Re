"""Does the consensus captured before a print equal the one reported after it?

Pre-registration exists to preserve a figure that was believed unrecoverable:
once a company has reported, the consensus its result was judged against is
gone. That was true of Yahoo, whose period labels are relative to today, so a
reading taken after the print described the NEXT quarter.

The earnings feed is not built that way. `/api/epsdetails/` states the
reported quarter's own consensus (`estimate`) after the print — measured on
ADM 2026-08-09: an actual of 1.84 against an estimate of 1.42, which is
precisely the number pre-registration was invented to keep. If that number
equals what was captured beforehand, pre-registration costs ~600 requests per
run and changes no judgment; if it does not, the gap is the most important
thing this system has measured about its own inputs.

This module answers that and nothing else. It never writes to the ledger and
never changes a verdict — putting a figure read after the print into the
pre-registration record is exactly the contamination the record exists to
prevent.

One thing it cannot measure, and the reason to run it soon rather than later:
the feed keeps only a company's LATEST print, so a pre-registered row can be
checked until that company reports again and not afterwards. Rows older than
a quarter are unmeasurable for good.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional

from hawkeye.contracts.stocks import ConsensusSnapshot, SnapshotKind
from hawkeye.marketdata.whispers import WhispersRecord, WhispersUnavailable

# What counts as the same number. Both endpoints publish to a fixed grid — EPS
# to the cent, revenue in millions — so a difference below that grid is the
# feed rounding, not analysts changing their minds. Without these a rounding
# artefact reads as drift, and the measurement returns "it always moves":
# the one answer that would keep pre-registration for no reason at all.
_EPS_GRID = 0.005                 # half a cent
_REVENUE_GRID_PCT = 0.01          # 0.01% of the figure


class DriftStatus(str, Enum):
    """Why one pre-registered row could or could not be checked.

    Named rather than boolean because the ways it fails to be measurable are
    not failures of the measurement: a print that has not happened yet will
    be checkable next week, and a row with no pre-print figure never will be.
    """
    UNCHANGED = "unchanged"
    MOVED = "moved"
    NOT_YET_REPORTED = "not_yet_reported"
    QUARTER_MISMATCH = "quarter_mismatch"
    NOTHING_TO_COMPARE = "nothing_to_compare"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class DriftReading:
    """One pre-registered row, beside what the feed says after the print."""
    ticker: str
    fiscal_quarter: str
    status: DriftStatus
    captured_at: Optional[datetime] = None
    announced_at: Optional[datetime] = None
    # How far apart the two readings are. A one-day gap and a ten-day gap are
    # different measurements, and one pooled "they differed" figure that mixes
    # them says nothing about either.
    days_apart: Optional[int] = None
    eps_before: Optional[float] = None
    eps_after: Optional[float] = None
    revenue_before: Optional[float] = None
    revenue_after: Optional[float] = None

    @property
    def eps_drift_pct(self) -> Optional[float]:
        return _pct(self.eps_before, self.eps_after)

    @property
    def revenue_drift_pct(self) -> Optional[float]:
        return _pct(self.revenue_before, self.revenue_after)


@dataclass(frozen=True)
class DriftReport:
    readings: list[DriftReading] = field(default_factory=list)

    def _count(self, status: DriftStatus) -> int:
        return sum(1 for r in self.readings if r.status is status)

    @property
    def unchanged(self) -> int:
        return self._count(DriftStatus.UNCHANGED)

    @property
    def moved(self) -> int:
        return self._count(DriftStatus.MOVED)

    @property
    def unreadable(self) -> int:
        return self._count(DriftStatus.UNREADABLE)

    @property
    def compared(self) -> int:
        """Rows where both figures existed. The denominator of every rate
        below, and the number that says whether the run measured anything."""
        return self.unchanged + self.moved

    def as_dict(self) -> dict:
        return {"compared": self.compared, "unchanged": self.unchanged,
                "moved": self.moved,
                "not_yet_reported": self._count(DriftStatus.NOT_YET_REPORTED),
                "quarter_mismatch": self._count(DriftStatus.QUARTER_MISMATCH),
                "nothing_to_compare":
                    self._count(DriftStatus.NOTHING_TO_COMPARE),
                "unreadable": self.unreadable}


def _pct(before: Optional[float], after: Optional[float]) -> Optional[float]:
    if before is None or after is None or before == 0:
        return None
    return round((after - before) / abs(before) * 100.0, 4)


def _same(before: Optional[float], after: Optional[float],
          grid: float, relative: bool) -> bool:
    """Whether two readings of one figure are the same number.

    A pair where either side is missing counts as the same: the caller has
    already established that at least one figure IS comparable, and a company
    with an EPS consensus and no revenue consensus must not be reported as
    having moved on revenue.
    """
    if before is None or after is None:
        return True
    tolerance = abs(before) * grid / 100.0 if relative else grid
    return abs(after - before) <= tolerance


def _comparable(snapshot: ConsensusSnapshot,
                record: WhispersRecord) -> Optional[DriftStatus]:
    """The reason this pair cannot be compared, or None when it can.

    The join is the fiscal quarter, which is the key pre-registration filed
    the row under. Report-date proximity is not enough: the feed answers with
    a company's latest print, so a company that has not reported yet returns
    LAST quarter — and comparing against that measures the gap between two
    different quarters and calls it estimate drift.
    """
    if record is None or not record.fiscal_quarter:
        return DriftStatus.NOT_YET_REPORTED
    if record.fiscal_quarter != snapshot.fiscal_quarter:
        # An OLDER quarter means this print has not been ingested yet and the
        # row is worth re-checking; a newer one means it never will be.
        return (DriftStatus.NOT_YET_REPORTED
                if record.fiscal_quarter < snapshot.fiscal_quarter
                else DriftStatus.QUARTER_MISMATCH)
    if snapshot.eps_avg is None and snapshot.revenue_avg is None:
        return DriftStatus.NOTHING_TO_COMPARE
    if record.eps_consensus is None and record.revenue_consensus is None:
        return DriftStatus.NOTHING_TO_COMPARE
    return None


def _read(snapshot: ConsensusSnapshot, source) -> DriftReading:
    common = dict(ticker=snapshot.ticker,
                  fiscal_quarter=snapshot.fiscal_quarter,
                  captured_at=snapshot.captured_at,
                  eps_before=snapshot.eps_avg,
                  revenue_before=snapshot.revenue_avg)
    try:
        record = source.details(snapshot.ticker)
    except WhispersUnavailable:
        return DriftReading(status=DriftStatus.UNREADABLE, **common)
    refusal = _comparable(snapshot, record)
    if refusal is not None:
        return DriftReading(status=refusal, **common)

    days = None
    if record.announced_at is not None and snapshot.captured_at is not None:
        days = (record.announced_at.date() - snapshot.captured_at.date()).days
    moved = not (_same(snapshot.eps_avg, record.eps_consensus,
                       _EPS_GRID, relative=False)
                 and _same(snapshot.revenue_avg, record.revenue_consensus,
                           _REVENUE_GRID_PCT, relative=True))
    return DriftReading(
        status=DriftStatus.MOVED if moved else DriftStatus.UNCHANGED,
        announced_at=record.announced_at, days_apart=days,
        eps_after=record.eps_consensus,
        revenue_after=record.revenue_consensus, **common)


def measure_consensus_drift(store, source, today: Optional[date] = None,
                            limit: Optional[int] = None) -> DriftReport:
    """Compare every pre-registered row whose print is due against the feed.

    Only rows whose expected report date has arrived cost a request: one for a
    print still days away cannot have moved yet, and asking about it spends a
    request to learn nothing.

    Reconstructions are excluded outright. They were written after the print
    from the same response this would compare them against, so including them
    would report a match rate of 100% that means nothing at all.
    """
    day = today or date.today()
    due = [s for s in store.pre_registered_snapshots()
           if s.expected_report_date is not None
           and s.expected_report_date <= day]
    due.sort(key=lambda s: (s.expected_report_date, s.ticker))
    if limit is not None:
        due = due[:max(limit, 0)]
    return DriftReport(readings=[_read(s, source) for s in due])


def report_line(report: DriftReport) -> str:
    """One-line Japanese summary for the CLI."""
    counts = report.as_dict()
    if report.compared == 0:
        head = ("コンセンサスの前後比較: 比較できた組が0件でした"
                "(決算前に記録した予想と、決算後にサイトが返す予想の"
                "両方が揃った銘柄がありません)")
    else:
        rate = report.unchanged / report.compared * 100.0
        head = (f"コンセンサスの前後比較: 比較できた {report.compared} 件 / "
                f"変わらなかった {report.unchanged} 件 / "
                f"動いた {report.moved} 件 (一致率 {rate:.1f}%)")
    return (f"{head} / まだ決算が出ていない {counts['not_yet_reported']} 件 / "
            f"別の四半期の決算が返ってきた {counts['quarter_mismatch']} 件 / "
            f"比べる数字が無い {counts['nothing_to_compare']} 件 / "
            f"サイトを読めず {counts['unreadable']} 件")
