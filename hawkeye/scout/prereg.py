"""Pre-registering consensus for prints that have not happened yet
(docs/design/MASTER_OVERVIEW.ja.md §5.3(4) / §6.1(D)).

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
    resolve_fiscal_quarter,
)
from hawkeye.marketdata.whispers import WhispersUnavailable
from hawkeye.scout.triage import is_investigation_target


class ConsensusProvider(Protocol):
    def forecast(self, ticker: str):
        """A WhispersForecast, or None when the feed holds no row for this
        company. Raises WhispersUnavailable when it could not be read at all —
        the two are never conflated, because only the first is a fact about
        the company (invariant 6)."""
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
    # The feed answered, and had no row for that company. Its calendar
    # estimate is still pre-registered.
    consensus_missing: int = 0
    # The feed could not be read at all. A fact about the connection, not
    # about the company, and counted apart so a run that reached nothing
    # cannot look like a day with no estimates (invariant 6).
    consensus_unreachable: int = 0
    skipped_already_reported: int = 0
    nothing_to_record: int = 0
    # Names the entry gates have already refused structurally (§6.1(E)).
    # Reported rather than silently dropped: this is the count that says how
    # much the filter is actually buying, and the count that would reveal it
    # excluding too much.
    skipped_not_target: int = 0
    # Prints the calendar gave no fiscal label for (EW移行 §2). Refused
    # rather than filed under a guessed label — but counted, because this is
    # the number that says how much pre-registration the omission is costing.
    skipped_unlabelled: int = 0
    tickers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"captured": self.captured, "unchanged": self.unchanged,
                "consensus_missing": self.consensus_missing,
                "consensus_unreachable": self.consensus_unreachable,
                "skipped_already_reported": self.skipped_already_reported,
                "nothing_to_record": self.nothing_to_record,
                "skipped_not_target": self.skipped_not_target,
                "skipped_unlabelled": self.skipped_unlabelled,
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


def capture_window(today: date, last_capture_at: Optional[datetime],
                   business_days: int, gap_days: int) -> list[date]:
    """The report dates one capture run covers.

    Normally tomorrow onward, because today's US prints land this evening
    JST and yesterday's run already pre-registered them. After a gap they
    are added back: the window is computed from the machine's LOCAL date
    (JST) while the calendar's dates are US market dates, and
    `business_days_ahead` always starts from tomorrow — so a morning run on
    day D asks about D+1 and D+2, and US day D's prints (which begin around
    20:00 JST that same evening) are never captured by it.

    Including today is safe by construction rather than by care: a calendar
    row carrying an actual is refused below, so a print that has already
    happened can never enter as a "pre-registration".
    """
    ahead = business_days_ahead(today, business_days)
    if last_capture_at is None:
        return [today] + ahead
    since = (today - last_capture_at.date()).days
    return ([today] + ahead) if since >= gap_days else ahead


def _fiscal_quarter(row: dict) -> str:
    """The calendar's own fiscal label, or "" when it gave none.

    "" rather than the calendar quarter of the report date: that fallback
    named the quarter AFTER the one being reported for anyone whose period
    does not end in the month they announce it, and a pre-registration filed
    under the wrong label is never found again by the print it belongs to
    (EW移行 §2). `capture_consensus` is where an unlabelled row is refused.
    """
    return resolve_fiscal_quarter(source_year=row.get("year"),
                                  source_quarter=row.get("quarter")).label


def upcoming_prints(raw: list[dict], today: date, business_days: int,
                    include_today: bool = False) -> list[UpcomingPrint]:
    """Calendar rows for releases still ahead of us, inside the window.

    A row that already carries an actual is excluded: capturing consensus
    after the fact is reconstruction, and pooling the two would make
    "pre-registered" an unverifiable claim (§6.1 要点2). That exclusion is
    also what makes `include_today` safe — see `capture_window`.
    """
    window = set(business_days_ahead(today, business_days))
    if include_today:
        window.add(today)
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
            fiscal_quarter=_fiscal_quarter(row),
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
                      kind: SnapshotKind = SnapshotKind.PRE_REGISTERED,
                      today: Optional[date] = None,
                      config=None) -> CaptureReport:
    """Record what each source expects of every print in `prints`.

    Both readings go into ONE row: they are two opinions about the same
    quantity taken at the same moment, and separating them would invite a
    later "which one is right" pick — the judgment this design exists to
    remove. Where the earnings feed has nothing the Finnhub point estimate is
    still pre-registered, with the absence visible in the row rather than
    inferred from silence.

    The feed's reading is the one the print's actual will later be compared
    against, and it comes from the same vendor as that actual — which is the
    reason this stopped being a Yahoo lookup on 2026-08-09. What the change
    gave up is the analyst count and the estimate range, neither of which
    exists on this endpoint and both of which were already decided not to be
    used; those columns now stay blank.
    """
    captured = unchanged = missing = skipped = empty = not_target = 0
    unlabelled = unreachable = 0
    touched: list[str] = []
    filtering = bool(config is not None
                     and getattr(config, "prereg_skip_non_targets", False))
    day = today or date.today()
    for item in prints:
        # No fiscal label, no row. The label is the only thing that joins this
        # snapshot to the print it is about (EW移行 §2), so a row written
        # without one is unreachable for ever — and a guessed label is worse,
        # because it joins to the WRONG print without ever looking wrong.
        if not item.fiscal_quarter:
            unlabelled += 1
            continue
        # Checked BEFORE the master row is created, so a name the gates have
        # already refused costs neither a lookup nor a write. An unknown name
        # is always a target — see hawkeye/scout/triage.py for why unknown
        # must never read as "no".
        if filtering and not is_investigation_target(
                store.stock_by_ticker(item.ticker), day, config):
            not_target += 1
            continue
        stock_id = resolve_stock(store, item.ticker, directory)
        if store.active_print(stock_id, item.fiscal_quarter) is not None:
            skipped += 1
            continue
        reading = None
        if source is not None:
            try:
                reading = source.forecast(item.ticker)
            except WhispersUnavailable:
                # Nothing came back, so nothing was learned about this
                # company. The calendar's estimate is still pre-registered —
                # a snapshot skipped today cannot be retaken tomorrow.
                unreachable += 1
            else:
                if reading is None:
                    missing += 1
        # No guidance yardstick is written here, and none can be: this
        # endpoint states the consensus for the print about to happen, so the
        # bar for the guidance that print will GIVE does not exist yet. It is
        # read afterwards, off the summary the print itself carries.
        snapshot = ConsensusSnapshot(
            stock_id=stock_id, ticker=item.ticker,
            fiscal_quarter=item.fiscal_quarter,
            captured_at=captured_at or datetime.now().astimezone(),
            kind=kind, expected_report_date=item.report_date,
            eps_calendar=item.eps_estimate,
            revenue_calendar=item.revenue_estimate,
            eps_avg=reading.eps_estimate if reading else None,
            revenue_avg=reading.revenue_estimate if reading else None,
            source_note="whispers+finnhub" if reading else "finnhub_only")
        # A row with no numbers in it is not "the estimate held still" — it
        # is nothing at all, and it would make the master look covered where
        # it is blank. A live two-day window carries ~560 names, plenty of
        # them with neither a calendar estimate nor a Yahoo reading.
        # An unstated label counts as nothing, the same as a missing number:
        # the key carries a period string as well as figures now, and a row
        # holding only `""` is still a row holding nothing.
        if all(v is None or v == "" for v in snapshot.content_key()[1:]):
            empty += 1
            continue
        stored_id = store.capture_consensus(snapshot)
        if stored_id == snapshot.id:
            captured += 1
            touched.append(item.ticker)
        else:
            unchanged += 1
    return CaptureReport(captured=captured, unchanged=unchanged,
                         consensus_missing=missing,
                         consensus_unreachable=unreachable,
                         skipped_already_reported=skipped,
                         nothing_to_record=empty,
                         skipped_not_target=not_target,
                         skipped_unlabelled=unlabelled, tickers=touched)


def report_line(report: CaptureReport) -> str:
    """One-line Japanese summary for the CLI."""
    return (f"事前登録: 新規 {report.captured} 件 / 変化なし "
            f"{report.unchanged} 件 / 決算専門サイトに予想の行がなく"
            f"カレンダーの数字のみ {report.consensus_missing} 件 / "
            f"決算専門サイトを読めず {report.consensus_unreachable} 件 / "
            f"発表済みのため対象外 {report.skipped_already_reported} 件 / "
            f"予想が1つも取れず記録なし {report.nothing_to_record} 件 / "
            f"入口ゲートの構造的条件を満たさず調査対象外 "
            f"{report.skipped_not_target} 件 / "
            f"四半期ラベルが決まらず保留 {report.skipped_unlabelled} 件")


def warn_if_nothing_captured(report: CaptureReport) -> None:
    """A run that captured nothing is either "no prints due" or "the source
    never answered", and those must not read the same way."""
    if report.captured == 0 and report.unchanged == 0:
        print("事前登録できたコンセンサスがありません(対象銘柄なし、"
              "またはデータ取得失敗)", file=sys.stderr)
