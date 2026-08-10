"""The quarters BEFORE the print that made a name a candidate (task 10).

A name that has just been discovered has exactly one quarter on record, and a
single beat says almost nothing: four consecutive beats and one beat after
seven misses lead to opposite conclusions, and the Adversary cannot mount
"the last two quarters missed" without the last two quarters.

What the free tier will actually give us, probed live on 2026-08-10 (AAPL and
MSFT) rather than assumed:

- The per-symbol history endpoint (`stock/earnings`) answers with FOUR rows
  whatever `limit` asks for. Eight quarters are not available at any price we
  are paying, so the run of eight is out of reach and the code does not
  pretend otherwise.
- Those rows carry EPS only — `actual` and `estimate`, no revenue columns.
  The one endpoint that does carry revenue is the earnings calendar, and a
  past window there returns zero rows on this key. So a backfilled quarter
  is a ONE-leg record, and `revenue_actual` stays None rather than being
  reconstructed from somewhere else.
- One of AAPL's four rows was a duplicate: 2026-Q2 and 2026-Q3 both reported
  an actual of 1.91 against different estimates. Nothing in the payload says
  which is stale. Both are flagged instead of one being picked (invariant 6 —
  contradictory data must not read as verified).

`estimate` is the calendar's single point estimate, so it is stored in
`eps_calendar` and never in `eps_avg`. Putting it in `eps_avg` would make it
indistinguishable from a real distribution, and the surprise screen's
denominator depends on that difference.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Optional, Protocol

from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    EarningsPrint,
    PrintSource,
    SnapshotKind,
    resolve_fiscal_quarter,
)

# Why a row the feed returned could not become a stored quarter. Counted by
# reason rather than as one total: "the vendor sent no number" and "we could
# not label the quarter" call for different actions, and a single count of
# skipped rows hides which of the two is happening.
BACKFILL_SKIP_REASONS = ("no_fiscal_quarter", "no_period_date", "no_actual")

# Two of the four rows described the same actual under different quarter
# labels. Kept as a flag on BOTH rows, because the payload does not say which
# one is the stale copy.
REPEATED_ACTUAL = "repeated_actual"
# The endpoint gives the quarter's END, not the day the company announced it.
# Recording the period end in `report_date` without saying so would put a date
# roughly four weeks early in front of the reader as an announcement date.
PERIOD_END_DATE = "report_date_is_period_end"

_SOURCE_NOTE = "finnhub_backfill"


class HistorySource(Protocol):
    def earnings_history(self, ticker: str,
                         limit: int = 4) -> Optional[list[dict]]: ...


@dataclass(frozen=True)
class HistoryQuarter:
    """One past quarter as the feed described it, before it is written.

    Rows that cannot be used are still returned, carrying `skip_reason`. A
    parser that dropped them would leave the caller unable to tell "the feed
    sent four unusable rows" from "the feed sent nothing".
    """
    fiscal_quarter: str = ""
    period_end: Optional[date] = None
    eps_actual: Optional[float] = None
    eps_estimate: Optional[float] = None
    flags: tuple[str, ...] = ()
    skip_reason: str = ""


@dataclass
class BackfillStats:
    """What one scan's backfill managed, in the kinds that call for action."""
    tickers_attempted: int = 0
    tickers_unreachable: int = 0      # the call did not complete
    quarters_written: int = 0
    quarters_already_known: int = 0   # a better row was already there
    skipped: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"backfill_tickers": self.tickers_attempted,
                "backfill_unreachable": self.tickers_unreachable,
                "backfill_quarters_written": self.quarters_written,
                "backfill_quarters_already_known": self.quarters_already_known,
                **{f"backfill_skipped_{k}": v
                   for k, v in sorted(self.skipped.items())}}


def parse_history(rows: list[dict]) -> list[HistoryQuarter]:
    """The feed's history rows, newest quarter first.

    Labelling goes through the shared resolver rather than formatting
    `year`/`quarter` here, so this stays one of the fixed bases instead of a
    private fourth rule that can drift from the others (EW移行 §2).
    """
    parsed = [_one(row) for row in rows]
    repeated = _repeated_actuals(parsed)
    parsed = [replace(q, flags=q.flags + (REPEATED_ACTUAL,))
              if q.eps_actual is not None and round(q.eps_actual, 6) in repeated
              else q
              for q in parsed]
    usable = sorted((q for q in parsed if not q.skip_reason),
                    key=lambda q: q.period_end, reverse=True)
    return usable + [q for q in parsed if q.skip_reason]


def _one(row: dict) -> HistoryQuarter:
    label = resolve_fiscal_quarter(source_year=row.get("year"),
                                   source_quarter=row.get("quarter")).label
    period_end = _day(row.get("period"))
    actual = row.get("actual")
    reason = ("no_fiscal_quarter" if not label else
              "no_period_date" if period_end is None else
              "no_actual" if actual is None else "")
    return HistoryQuarter(fiscal_quarter=label, period_end=period_end,
                          eps_actual=actual, eps_estimate=row.get("estimate"),
                          skip_reason=reason)


def _repeated_actuals(quarters: list[HistoryQuarter]) -> set[float]:
    """Actuals that appear under more than one quarter label.

    Two quarters cannot have reported the identical figure and also both be
    right about which quarter it belongs to — one of them is the vendor
    repeating itself, and there is no field that says which.
    """
    by_actual: dict[float, set[str]] = {}
    for q in quarters:
        if q.eps_actual is None or not q.fiscal_quarter:
            continue
        by_actual.setdefault(round(q.eps_actual, 6), set()).add(q.fiscal_quarter)
    return {actual for actual, labels in by_actual.items() if len(labels) > 1}


def _day(value) -> Optional[date]:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def backfill_history(store, source: HistorySource,
                     targets: list[tuple[str, str]],
                     quarters: int = 4) -> BackfillStats:
    """Fill in past quarters for `targets`, one request per ticker.

    `targets` is (ticker, stock_id) for the shortlist only. Asking for every
    screened name would cost hundreds of requests to enrich companies no
    argument will ever be made about.

    A quarter already on record is left exactly as it is, never overwritten:
    the row that is there came from a full reading with revenue and a
    consensus in force, and this one has EPS alone. Replacing the better row
    with the thinner one is a loss disguised as an update.
    """
    stats = BackfillStats()
    for ticker, stock_id in targets:
        stats.tickers_attempted += 1
        rows = source.earnings_history(ticker, limit=quarters)
        if rows is None:
            stats.tickers_unreachable += 1
            continue
        parsed = parse_history(rows)
        for entry in parsed:
            if entry.skip_reason:
                stats.skipped[entry.skip_reason] = (
                    stats.skipped.get(entry.skip_reason, 0) + 1)
        # Sliced here as well as in the request: the endpoint ignores `limit`
        # (four rows come back whatever it says), so this is the only place the
        # cap actually holds.
        for entry in [q for q in parsed if not q.skip_reason][:quarters]:
            if store.active_print(stock_id, entry.fiscal_quarter) is not None:
                stats.quarters_already_known += 1
                continue
            _write(store, stock_id, ticker, entry)
            stats.quarters_written += 1
    return stats


def _write(store, stock_id: str, ticker: str, entry: HistoryQuarter) -> None:
    snapshot_id = ""
    if entry.eps_estimate is not None:
        snapshot_id = store.capture_consensus(ConsensusSnapshot(
            stock_id=stock_id, ticker=ticker,
            fiscal_quarter=entry.fiscal_quarter,
            kind=SnapshotKind.RECONSTRUCTED,
            eps_calendar=entry.eps_estimate,
            source_note=_SOURCE_NOTE))
    store.record_print(EarningsPrint(
        stock_id=stock_id, ticker=ticker,
        fiscal_quarter=entry.fiscal_quarter,
        report_date=entry.period_end,
        source=PrintSource.FINNHUB,
        eps_actual=entry.eps_actual,
        consensus_snapshot_id=snapshot_id,
        contamination_flags=[PERIOD_END_DATE, *entry.flags],
        notes=_SOURCE_NOTE))
