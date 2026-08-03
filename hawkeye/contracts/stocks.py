"""Stock-centric contracts: the master, its quarterly prints, and the
consensus that was in force before each of them (docs/MASTER_OVERVIEW.ja.md
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
3. **A print deepens by appending a row, not by rewriting one.** `depth`
   exists so "we never looked at this quarter" and "we looked and found
   nothing" stay distinguishable (invariant 6); rewriting the shallow row
   would erase exactly that difference.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

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


class PrintDepth(str, Enum):
    CALENDAR_ONLY = "calendar_only"      # free: the calendar response itself
    VERIFIED = "verified"                # both sources' actuals compared
    XBRL_VALIDATED = "xbrl_validated"    # checked against EDGAR XBRL
    RELEASE_READ = "release_read"        # the release itself was read

    @property
    def rank(self) -> int:
        return _DEPTH_ORDER.index(self)


_DEPTH_ORDER = [PrintDepth.CALENDAR_ONLY, PrintDepth.VERIFIED,
                PrintDepth.XBRL_VALIDATED, PrintDepth.RELEASE_READ]


class EpsBasis(str, Enum):
    """Which basis an EPS actual is on. `UNADJUSTED` is not a failure — it is
    the honest label for "the company published no non-GAAP figure and named
    no one-off", and it travels to the tribunal as a known unknown. Non-GAAP
    is READ, never computed (§5.3 decision 1)."""
    AS_REPORTED = "as_reported"          # GAAP, no one-off identified
    ADJUSTED = "adjusted"                # the company's own non-GAAP figure
    UNADJUSTED = "unadjusted"            # GAAP, and a one-off may be inside it


def fiscal_quarter_of(day: date) -> str:
    """Calendar-quarter fallback label, `2026-Q3`.

    Only used when the source gives no fiscal labelling of its own; the
    calendar and the filings do carry one, and that is preferred, because a
    company with a non-December year end reports its Q1 in what is calendar
    Q2.
    """
    return f"{day.year}-Q{(day.month - 1) // 3 + 1}"


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

    Yahoo supplies a distribution (mean/low/high/analyst count) and Finnhub a
    single point, so the two-source confirmation is asymmetric by
    construction — that asymmetry is recorded, not smoothed over.
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
    eps_finnhub: Optional[float] = None

    revenue_avg: Optional[float] = None
    revenue_low: Optional[float] = None
    revenue_high: Optional[float] = None
    revenue_analysts: Optional[int] = None
    revenue_finnhub: Optional[float] = None

    # The yardstick guidance is judged against (§5.3 decision 3): next
    # quarter's consensus, captured at the same moment as this quarter's.
    next_quarter_eps_avg: Optional[float] = None
    next_quarter_revenue_avg: Optional[float] = None

    source_note: str = ""

    def content_key(self) -> tuple:
        """The values that decide whether a capture adds information.

        `captured_at` and `kind` are excluded on purpose: re-capturing
        unchanged numbers an hour later is not new information, and writing a
        row for it would bury the captures that DO record a moved estimate.
        """
        return (self.fiscal_quarter, self.eps_avg, self.eps_low, self.eps_high,
                self.eps_analysts, self.eps_finnhub, self.revenue_avg,
                self.revenue_low, self.revenue_high, self.revenue_analysts,
                self.revenue_finnhub, self.next_quarter_eps_avg,
                self.next_quarter_revenue_avg)


class EarningsPrint(BaseModel):
    """One quarter's reported figures, at one depth of examination.

    `eps_finnhub` is a list because the calendar can return several rows for
    one print carrying different actuals (AMZN: 1.88 and 1.97). When they
    disagree, Finnhub's actual is unusable for that print — picking the row
    that happens to match Yahoo is exactly the "choose the more plausible
    one" judgment this system exists to remove.
    """
    id: str = Field(default_factory=lambda: new_id("ern"))
    stock_id: str
    ticker: str = ""
    fiscal_quarter: str
    report_date: date
    reported_at: Optional[datetime] = None
    depth: PrintDepth = PrintDepth.CALENDAR_ONLY
    recorded_at: datetime = Field(default_factory=now)

    eps_yahoo: Optional[float] = None
    eps_finnhub: list[float] = Field(default_factory=list)
    eps_xbrl_diluted: Optional[float] = None
    eps_release: Optional[float] = None        # read from the release itself
    eps_basis: EpsBasis = EpsBasis.AS_REPORTED
    one_off_per_share: Optional[float] = None  # only if the company named it

    revenue_finnhub: Optional[float] = None
    revenue_xbrl: Optional[float] = None
    revenue_release: Optional[float] = None

    guidance: Optional[GuidanceReading] = None
    contamination_flags: list[str] = Field(default_factory=list)
    consensus_snapshot_id: str = ""
    notes: str = ""

    @property
    def eps_finnhub_usable(self) -> Optional[float]:
        """Finnhub's actual, or None when its own rows contradict each other."""
        distinct = {round(v, 6) for v in self.eps_finnhub}
        return self.eps_finnhub[0] if len(distinct) == 1 else None

    @property
    def revenue_actual(self) -> Optional[float]:
        """XBRL first: Finnhub's revenue matched the filings 22/22, so the
        two agreeing is the normal case and XBRL is the one with a primary
        source behind it."""
        for value in (self.revenue_xbrl, self.revenue_release,
                      self.revenue_finnhub):
            if value is not None:
                return value
        return None
