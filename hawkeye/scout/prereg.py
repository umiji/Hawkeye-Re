"""Pre-registering consensus for prints that have not happened yet
(docs/MASTER_OVERVIEW.ja.md §5.3(4) / §6.1(D)).

Why this runs before the release and not after: consensus has no primary
source anywhere in the world, so two vendors disagreeing about it can never
be settled by a document — only by having recorded, at a known moment, what
each of them said. Capture it late and the vintage question ("did they
disagree about method, or did they sample on different days?") becomes
permanently unanswerable. BIIB's estimate moved 3.98 -> 2.15 over ninety
days; that is the size of the effect being controlled for.

The window is business days rather than "tomorrow", because runs are manual:
one missed day would lose that print's snapshot forever, and no later run can
recover it.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional, Protocol

from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    SnapshotKind,
    Stock,
    fiscal_quarter_of,
)


class ConsensusProvider(Protocol):
    def consensus(self, ticker: str):
        """A ConsensusReading, or None. None always means "not available"."""
        ...


@dataclass(frozen=True)
class UpcomingPrint:
    """A scheduled release, with what the calendar expects of it."""
    ticker: str
    report_date: date
    fiscal_quarter: str
    eps_estimate: Optional[float] = None
    revenue_estimate: Optional[float] = None


@dataclass(frozen=True)
class CaptureReport:
    """What one capture run managed.

    `unchanged` and `captured` are reported apart because they mean opposite
    things: `unchanged` is evidence the estimate held still, while a run that
    captured nothing AND changed nothing never reached the source at all.
    """
    captured: int = 0
    unchanged: int = 0
    yahoo_missing: int = 0
    skipped_already_reported: int = 0
    tickers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"captured": self.captured, "unchanged": self.unchanged,
                "yahoo_missing": self.yahoo_missing,
                "skipped_already_reported": self.skipped_already_reported,
                "tickers": list(self.tickers)}


def business_days_ahead(today: date, count: int) -> list[date]:
    """The next `count` weekdays after `today`.

    Market holidays are not modelled: including one costs a wasted lookup,
    excluding it by accident would cost a snapshot that cannot be retaken.
    The asymmetry decides the simplification.
    """
    days: list[date] = []
    day = today
    while len(days) < max(count, 0):
        day += timedelta(days=1)
        if day.weekday() < 5:
            days.append(day)
    return days


def _fiscal_quarter(row: dict, day: date) -> str:
    """The source's own fiscal label when it has one.

    The fallback is the calendar quarter, which is wrong for a company whose
    fiscal year does not end in December — so it is only reached when the
    calendar row carries no year/quarter at all.
    """
    year, quarter = row.get("year"), row.get("quarter")
    if year and quarter:
        try:
            return f"{int(year)}-Q{int(quarter)}"
        except (TypeError, ValueError):
            pass
    return fiscal_quarter_of(day)


def upcoming_prints(raw: list[dict], today: date,
                    business_days: int) -> list[UpcomingPrint]:
    """Calendar rows for releases still ahead of us, inside the window.

    A row that already carries an actual is excluded: capturing consensus
    after the fact is reconstruction, and pooling the two would make
    "pre-registered" an unverifiable claim (§6.1 要点2).
    """
    window = set(business_days_ahead(today, business_days))
    out: list[UpcomingPrint] = []
    for row in raw:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol or "." in symbol:
            continue
        try:
            day = date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if day not in window or row.get("epsActual") is not None:
            continue
        out.append(UpcomingPrint(
            ticker=symbol, report_date=day,
            fiscal_quarter=_fiscal_quarter(row, day),
            eps_estimate=row.get("epsEstimate"),
            revenue_estimate=row.get("revenueEstimate")))
    out.sort(key=lambda p: (p.report_date, p.ticker))
    return out


def resolve_stock(store, ticker: str, directory=None) -> str:
    """The master row for a ticker, created if this is the first sighting.

    An existing row wins whenever EDGAR cannot supply a CIK, so a temporary
    directory outage does not spawn a provisional twin of a company already
    keyed properly.
    """
    cik = directory.cik_for(ticker) if directory is not None else None
    existing = store.stock_by_ticker(ticker)
    if cik is None and existing is not None:
        return existing.id
    name = directory.name_for(ticker) if directory is not None else ""
    return store.put_stock(Stock(cik=cik, ticker=ticker,
                                 name=name or (existing.name if existing else "")))


def capture_consensus(store, prints: list[UpcomingPrint],
                      source: Optional[ConsensusProvider],
                      directory=None,
                      captured_at: Optional[datetime] = None,
                      kind: SnapshotKind = SnapshotKind.PRE_REGISTERED
                      ) -> CaptureReport:
    """Record what each source expects of every print in `prints`.

    Both readings go into ONE row: they are two opinions about the same
    quantity taken at the same moment, and separating them would invite a
    later "which one is right" pick — the judgment this design exists to
    remove. Where Yahoo cannot be reached the Finnhub point estimate is still
    pre-registered, with the absent distribution visible in the row rather
    than inferred from silence.
    """
    captured = unchanged = missing = skipped = 0
    touched: list[str] = []
    for item in prints:
        stock_id = resolve_stock(store, item.ticker, directory)
        if store.latest_print(stock_id, item.fiscal_quarter) is not None:
            skipped += 1
            continue
        reading = source.consensus(item.ticker) if source is not None else None
        if reading is None:
            missing += 1
        snapshot = ConsensusSnapshot(
            stock_id=stock_id, ticker=item.ticker,
            fiscal_quarter=item.fiscal_quarter,
            captured_at=captured_at or datetime.now().astimezone(),
            kind=kind, expected_report_date=item.report_date,
            eps_finnhub=item.eps_estimate,
            revenue_finnhub=item.revenue_estimate,
            eps_avg=reading.eps_avg if reading else None,
            eps_low=reading.eps_low if reading else None,
            eps_high=reading.eps_high if reading else None,
            eps_analysts=reading.eps_analysts if reading else None,
            revenue_avg=reading.revenue_avg if reading else None,
            revenue_low=reading.revenue_low if reading else None,
            revenue_high=reading.revenue_high if reading else None,
            revenue_analysts=reading.revenue_analysts if reading else None,
            next_quarter_eps_avg=(reading.next_quarter_eps_avg
                                  if reading else None),
            next_quarter_revenue_avg=(reading.next_quarter_revenue_avg
                                      if reading else None),
            source_note="yahoo+finnhub" if reading else "finnhub_only")
        stored_id = store.capture_consensus(snapshot)
        if stored_id == snapshot.id:
            captured += 1
            touched.append(item.ticker)
        else:
            unchanged += 1
    return CaptureReport(captured=captured, unchanged=unchanged,
                         yahoo_missing=missing,
                         skipped_already_reported=skipped, tickers=touched)


def report_line(report: CaptureReport) -> str:
    """One-line Japanese summary for the CLI."""
    return (f"事前登録: 新規 {report.captured} 件 / 変化なし "
            f"{report.unchanged} 件 / Yahoo未取得 {report.yahoo_missing} 件 / "
            f"発表済みのため対象外 {report.skipped_already_reported} 件")


def warn_if_nothing_captured(report: CaptureReport) -> None:
    """A run that captured nothing is either "no prints due" or "the source
    never answered", and those must not read the same way."""
    if report.captured == 0 and report.unchanged == 0:
        print("事前登録できたコンセンサスがありません(対象銘柄なし、"
              "またはデータ取得失敗)", file=sys.stderr)
