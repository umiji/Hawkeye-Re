"""Stock-centric contracts: the master, its quarterly prints, and the
consensus that was in force before each of them (docs/design/MASTER_OVERVIEW.ja.md
§6.1).

Three properties are load-bearing and are enforced by the storage layer
(`hawkeye/ledger/stocks.py`), not by convention:

1. **Identity is the SEC registrant number (CIK), never the ticker.** Tickers
   are reused after a delisting and change on a rename, so keying on one
   merges two companies' histories silently.
2. **A consensus row is written once and never changed.** A decision
   references the row BY ID instead of copying its numbers into the payload,
   so an update would repoint a pre-registered recommendation at different
   figures with nothing in the record to show it (invariant 1).
3. **A revised actual appends a row and retires the old one; it never
   rewrites it.** Overwriting would erase which figure a ranking was actually
   made on, and the drop review and the thesis-accuracy scoring both read
   that.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from enum import Enum
from typing import NamedTuple, Optional

from pydantic import BaseModel, Field, model_validator

from hawkeye.contracts.models import new_id, now


class ReviewStage(str, Enum):
    """How far a stock got the last time it was looked at."""
    SCREENED = "screened"                # passed the surprise screen
    GATE_REJECT = "gate_reject"          # rejected by the entry gates
    TRIBUNAL_PASS = "tribunal_pass"      # argued, and the judge declined
    BUY = "buy"


class SnapshotKind(str, Enum):
    """Pre-registered and after-the-fact estimates must never be pooled: the
    moment they are, "this is what was knowable before the print" becomes an
    unverifiable claim."""
    PRE_REGISTERED = "pre_registered"    # captured before the release
    RECONSTRUCTED = "reconstructed"      # captured afterwards; weaker evidence


class PrintSource(str, Enum):
    """Which vendor supplied the numbers one earnings row stands on.

    Always a VENDOR, never a conclusion. Nothing downstream of the ranking may
    ever write a third kind of value here: the moment a tribunal's finding can
    reach an earnings row, "what did we know when we ranked this name" stops
    being answerable and the pre-registration means nothing.

    This replaced `depth`, which recorded how hard anyone had looked. That was
    worth holding while four escalations could each deepen a quarter's row; with
    one source per print it said the same thing twice.

    Both figures on a row come from the vendor named here — actual AND the
    consensus it is measured against. A ratio built from one vendor's actual
    over another's consensus compares an adjusted-basis estimate against a
    possibly GAAP figure, which is why the choice is made once per print
    (hawkeye/scout/numbers.py).
    """
    FINNHUB = "finnhub"                  # the earnings calendar's own figures
    WHISPERS = "whispers"                # EarningsWhispers, read before ranking


class RowStatus(str, Enum):
    """Whether a recorded row is still the one that stands.

    A revised actual appends a new row and retires the old one rather than
    overwriting it. Keeping the retired row is what makes "we ranked ADEA on
    $0.34, and the figure became $0.42 the next day" a readable fact instead of
    a record that looks like we knew $0.42 all along.
    """
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class QuarterBasis(str, Enum):
    """What a fiscal-quarter label was derived from.

    Carried alongside the label because the three bases are not equally
    trustworthy, and a system that cannot tell them apart cannot later find
    out which of them was wrong.
    """
    FISCAL_REFERENCES = "fiscal_references"   # the company's own two dates
    SOURCE_LABEL = "source_label"             # the calendar's year/quarter
    QUARTER_END = "quarter_end"               # the period end, nothing else
    WITHHELD = "withheld"                     # no label, and why


class QuarterLabel(NamedTuple):
    """`2026-Q2` and where it came from; `label` is "" when it was withheld."""
    label: str
    basis: QuarterBasis
    reason: str = ""


_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_QUARTER_WORD = re.compile(r"\b(first|second|third|fourth)\s+quarter\b", re.I)


def _calendar_quarter_of(day: date) -> str:
    """`2026-Q2` — the calendar quarter the date falls in.

    Deliberately private. Applied to a PERIOD END it is right for a December
    fiscal year and wrong otherwise; applied to a REPORT DATE it is wrong for
    nearly everyone, because a quarter ending in June is reported in August.
    That second use is what `resolve_fiscal_quarter` exists to make
    unreachable, so nothing outside this module may call it.
    """
    return f"{day.year}-Q{(day.month - 1) // 3 + 1}"


def _from_fiscal_references(quarter_end: Optional[date],
                            year_end: Optional[date]) -> Optional[str]:
    """Counted back from the company's OWN year end, so a January year end
    puts a quarter ending April into the next fiscal year (NVDA: 2027-Q1,
    where the calendar would say 2026-Q2 — a quarter it never reports)."""
    if quarter_end is None or year_end is None:
        return None
    months_before = ((year_end.year * 12 + year_end.month)
                     - (quarter_end.year * 12 + quarter_end.month))
    if months_before % 3 or not 0 <= months_before <= 9:
        return None
    return f"{year_end.year}-Q{4 - months_before // 3}"


def _from_source_label(year, quarter) -> Optional[str]:
    if not year or not quarter:
        return None
    try:
        number, fiscal_year = int(quarter), int(year)
    except (TypeError, ValueError):
        return None
    return f"{fiscal_year}-Q{number}" if 1 <= number <= 4 else None


def resolve_fiscal_quarter(*,
                           quarter_end: Optional[date] = None,
                           year_end: Optional[date] = None,
                           quarter_text: str = "",
                           source_year=None,
                           source_quarter=None) -> QuarterLabel:
    """The one place a print's fiscal quarter is decided (EW移行 §2).

    Everything that joins a print to its consensus runs off this label —
    `consensus_in_force`, `already_reviewed`, and the unique index over the
    active rows — so two rows for one print, or a label one quarter ahead,
    silently breaks all three at once.

    The bases are tried in a fixed order and the winner records which one it
    was. There is deliberately NO parameter for the report date: labelling a
    print by the calendar quarter it was ANNOUNCED in lands one quarter ahead
    for anyone whose quarter does not end in the month they report it
    (measured 2026-08-07: 40 of 41 live records), and it is a mistake nothing
    downstream can detect.

    `quarter_text` is the feed's own prose ("second quarter ended June
    2026"). It is a second, independent statement of the same fact, so a
    contradiction means one of the two is wrong and neither may be used —
    the label is withheld rather than picked between (fail closed,
    invariant 6).
    """
    for label, basis in (
            (_from_fiscal_references(quarter_end, year_end),
             QuarterBasis.FISCAL_REFERENCES),
            (_from_source_label(source_year, source_quarter),
             QuarterBasis.SOURCE_LABEL),
            (_calendar_quarter_of(quarter_end) if quarter_end else None,
             QuarterBasis.QUARTER_END)):
        if label is None:
            continue
        stated = _QUARTER_WORD.search(quarter_text or "")
        if stated is not None and not label.endswith(
                f"-Q{_ORDINALS[stated.group(1).lower()]}"):
            return QuarterLabel("", QuarterBasis.WITHHELD,
                                "quarter_text_mismatch")
        return QuarterLabel(label, basis)
    return QuarterLabel("", QuarterBasis.WITHHELD, "no_fiscal_reference")


class Stock(BaseModel):
    """Slowly-changing facts about a company, plus a projection of the last
    review. Deliberately holds NO point-in-time observation (price, market
    cap, ratings): those are frozen inside the immutable decision payload,
    and a mutable "current price" here would be a path for later analysis to
    read a value the decision could not have known."""
    id: str = ""                          # `cik:0001018724` or `prov:TICKER`
    cik: Optional[str] = None
    ticker: str
    name: str = ""
    exchange: str = ""
    sector: str = ""
    industry: str = ""
    fiscal_year_end_month: Optional[int] = None
    listing_status: str = "listed"
    as_of: datetime = Field(default_factory=now)

    # -- is this company worth spending lookups on at all (§6.1(E))
    # None means nobody has judged it yet, and that is NOT "no": pre-
    # registration includes it. Only an explicit False excludes, and only
    # while it is recent — a snapshot not taken can never be taken, so a
    # verdict that never expired would make one bad day permanent. The
    # numbers behind the verdict are deliberately NOT stored (see the class
    # docstring); the reason names the gates, for a human to read.
    investigation_target: Optional[bool] = None
    investigation_reason: str = ""
    investigation_checked_at: Optional[datetime] = None

    # -- projection of the ledger; `hawkeye stocks rebuild` recreates it
    last_reviewed_fiscal_quarter: str = ""
    last_reviewed_at: Optional[datetime] = None
    last_stage_reached: Optional[ReviewStage] = None

    @model_validator(mode="after")
    def _fill_id(self) -> "Stock":
        if not self.id:
            object.__setattr__(self, "id", stock_id_for(self.cik, self.ticker))
        return self


def stock_id_for(cik: Optional[str], ticker: str) -> str:
    """A CIK-keyed id, or a stable provisional one when EDGAR has no match.

    The provisional id is deliberately derived from the ticker, so a stock
    that later gains a CIK gets a NEW id rather than silently inheriting the
    old rows — merging the two is a decision for a human, not a side effect.
    """
    if cik:
        return f"cik:{str(cik).strip().lstrip('0').zfill(10)}"
    return f"prov:{ticker.strip().upper()}"


class GuidanceReading(BaseModel):
    """Guidance as the company published it, read from the release.

    There is no structured source for this anywhere on the free tiers, so
    absence is the normal case and carries no penalty (§5.3 decision 3).
    """
    period: str = ""                      # `2026-Q3`, `FY2026`
    eps_low: Optional[float] = None
    eps_high: Optional[float] = None
    revenue_low: Optional[float] = None
    revenue_high: Optional[float] = None
    source_excerpt: str = ""              # the sentence it was read from

    @property
    def eps_midpoint(self) -> Optional[float]:
        return _midpoint(self.eps_low, self.eps_high)

    @property
    def revenue_midpoint(self) -> Optional[float]:
        return _midpoint(self.revenue_low, self.revenue_high)


def _midpoint(low: Optional[float], high: Optional[float]) -> Optional[float]:
    values = [v for v in (low, high) if v is not None]
    return sum(values) / len(values) if values else None


class ConsensusSnapshot(BaseModel):
    """What analysts expected, as of one moment. APPEND-ONLY.

    Fields are named for the ROLE a figure plays, not for the vendor it came
    from, because the vendor changes and the role does not:

    - `eps_avg` / `eps_low` / `eps_high` / `eps_analysts` — the distribution,
      from whichever source publishes one (Yahoo today, EarningsWhispers next).
    - `eps_calendar` — the earnings calendar's single point estimate. Kept
      apart because a point and a distribution are not the same evidence, and
      the asymmetry between them is what "both sources agree" is measured on.
    - `next_quarter_*` and `full_year_*` — the two yardsticks guidance can be
      judged against. Which one applies depends on the period the company
      guided for, and it is routinely the full year: 10 of the 47 measured
      names published a full-year range and no quarterly figure at all. With
      nowhere to put that, their guidance read as absent when they had in fact
      guided (EW移行 §5).
    """
    id: str = Field(default_factory=lambda: new_id("cns"))
    stock_id: str
    ticker: str = ""
    fiscal_quarter: str
    captured_at: datetime = Field(default_factory=now)
    kind: SnapshotKind = SnapshotKind.PRE_REGISTERED
    expected_report_date: Optional[date] = None

    eps_avg: Optional[float] = None
    eps_low: Optional[float] = None
    eps_high: Optional[float] = None
    eps_analysts: Optional[int] = None
    eps_calendar: Optional[float] = None

    revenue_avg: Optional[float] = None
    revenue_low: Optional[float] = None
    revenue_high: Optional[float] = None
    revenue_analysts: Optional[int] = None
    revenue_calendar: Optional[float] = None

    # The yardsticks guidance is judged against (§5.3 decision 3), captured at
    # the same moment as this quarter's consensus.
    next_quarter_eps_avg: Optional[float] = None
    next_quarter_revenue_avg: Optional[float] = None
    full_year_eps_avg: Optional[float] = None
    full_year_revenue_avg: Optional[float] = None

    source_note: str = ""

    def content_key(self) -> tuple:
        """The values that decide whether a capture adds information.

        `captured_at` and `kind` are excluded on purpose: re-capturing
        unchanged numbers an hour later is not new information, and writing a
        row for it would bury the captures that DO record a moved estimate.

        Every number IS included, and has to be: a figure left out of this key
        is one whose movement is silently discarded, which looks exactly like a
        figure that never moved.
        """
        return (self.fiscal_quarter, self.eps_avg, self.eps_low, self.eps_high,
                self.eps_analysts, self.eps_calendar, self.revenue_avg,
                self.revenue_low, self.revenue_high, self.revenue_analysts,
                self.revenue_calendar, self.next_quarter_eps_avg,
                self.next_quarter_revenue_avg, self.full_year_eps_avg,
                self.full_year_revenue_avg)


class EarningsPrint(BaseModel):
    """One quarter's reported figures, from one vendor, at one moment.

    Named for roles rather than vendors, same as the consensus row above:
    `eps_actual` is the figure this row stands on, whoever supplied it, and
    `source` says who that was.

    `eps_actual_rows` is a list because the earnings calendar can return
    several rows for one print carrying different actuals (AMZN: 1.88 and
    1.97). When they disagree the calendar's actual is unusable for that
    print — picking the row that happens to match another source is exactly
    the "choose the more plausible one" judgment this system exists to remove.

    A recorded row is never rewritten. A revised actual appends a new row and
    retires the old one (`status`), so what is true now and what a ranking was
    actually made on are both still readable.
    """
    id: str = Field(default_factory=lambda: new_id("ern"))
    stock_id: str
    ticker: str = ""
    fiscal_quarter: str
    report_date: date
    reported_at: Optional[datetime] = None
    source: PrintSource = PrintSource.FINNHUB
    status: RowStatus = RowStatus.ACTIVE
    recorded_at: datetime = Field(default_factory=now)

    eps_actual: Optional[float] = None
    eps_actual_rows: list[float] = Field(default_factory=list)

    revenue_actual: Optional[float] = None

    guidance: Optional[GuidanceReading] = None
    contamination_flags: list[str] = Field(default_factory=list)
    consensus_snapshot_id: str = ""
    notes: str = ""

    @property
    def eps_actual_rows_usable(self) -> Optional[float]:
        """The calendar's actual, or None when its own rows contradict it."""
        distinct = {round(v, 6) for v in self.eps_actual_rows}
        return self.eps_actual_rows[0] if len(distinct) == 1 else None
