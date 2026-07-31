"""Core data contracts shared across every Hawkeye service.

These models are the ONLY interface between services (scout, tribunal,
risk, ledger, sentinel, reports). Services never import each other's
internals. Anything persisted to the ledger is one of these models
serialized to JSON, which is what makes the record replayable and
auditable years later.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Candidate / market facts (input side — facts only, no opinion)
# ---------------------------------------------------------------------------

class CatalystType(str, Enum):
    EARNINGS_BEAT = "earnings_beat"          # machine-detected EPS/revenue surprise
    EARNINGS_BEAT_RAISE = "earnings_beat_raise"
    GUIDANCE_RAISE = "guidance_raise"
    EARNINGS_OVERREACTION = "earnings_overreaction"
    PRODUCT_LAUNCH = "product_launch"
    INSIDER_BUYING = "insider_buying"
    INDEX_INCLUSION = "index_inclusion"
    SPINOFF_RESTRUCTURING = "spinoff_restructuring"
    MERGER_ACQUISITION = "merger_acquisition"
    REGULATORY_APPROVAL = "regulatory_approval"
    OTHER = "other"


class Catalyst(BaseModel):
    type: CatalystType
    description: str
    event_date: date
    source: str = ""


class NewsItem(BaseModel):
    headline: str
    source: str = ""
    url: str = ""
    published_at: Optional[datetime] = None
    summary: str = ""


class MarketSnapshot(BaseModel):
    ticker: str
    as_of: datetime = Field(default_factory=utcnow)
    price: float
    prev_close: Optional[float] = None
    market_cap: Optional[float] = None             # USD
    avg_dollar_volume_20d: Optional[float] = None  # USD
    atr_pct_14d: Optional[float] = None            # 14d ATR / price * 100
    gap_on_event_pct: Optional[float] = None       # close-to-close move on catalyst day
    change_since_event_pct: Optional[float] = None
    days_since_event: Optional[int] = None         # trading days
    next_earnings_date: Optional[date] = None
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None
    eps_surprise_pct: Optional[float] = None       # structured, not just prose
    revenue_surprise_pct: Optional[float] = None


class InsiderActivity(BaseModel):
    """Net insider open-market buying/selling over a trailing window.

    Only transaction codes P (open-market purchase) and S (open-market
    sale) are counted — option exercises, grants, and tax withholding are
    noise for the sucker test ("who is selling to us, and why").
    """
    window_days: int
    net_shares: float          # positive = net insider buying
    buyers: int                # distinct insiders with net purchases
    sellers: int                # distinct insiders with net sales


class AnalystTrend(BaseModel):
    """Analyst recommendation counts, latest period vs. the prior one."""
    period: date
    strong_buy: int
    buy: int
    hold: int
    sell: int
    strong_sell: int
    prior_period: Optional[date] = None
    prior_strong_buy: Optional[int] = None
    prior_buy: Optional[int] = None
    prior_hold: Optional[int] = None
    prior_sell: Optional[int] = None
    prior_strong_sell: Optional[int] = None


class CandidateBrief(BaseModel):
    """Facts-only dossier handed to the tribunal. No recommendation, no spin."""
    id: str = Field(default_factory=lambda: new_id("cnd"))
    ticker: str
    company_name: str = ""
    sector: str = ""
    snapshot: MarketSnapshot
    catalyst: Catalyst
    news: list[NewsItem] = Field(default_factory=list)
    insider_activity: Optional[InsiderActivity] = None
    analyst_trend: Optional[AnalystTrend] = None
    notes: str = ""


# ---------------------------------------------------------------------------
# Entry gates (deterministic pre-checks)
# ---------------------------------------------------------------------------

class GateResult(BaseModel):
    name: str
    passed: bool
    hard: bool                       # hard gate failure kills the candidate pre-LLM
    unverified: bool = False         # data missing; passed provisionally with a warning
    value: Optional[float] = None
    threshold: Optional[float] = None
    note: str = ""


class GateReport(BaseModel):
    results: list[GateResult] = Field(default_factory=list)

    @property
    def hard_failures(self) -> list[GateResult]:
        # Fail closed: an unverified hard gate is data we don't have, not
        # data that cleared the bar. Letting it through would silently trade
        # away the liquidity/size/freshness floor the hard gate exists to
        # enforce (see docs/MASTER_OVERVIEW.ja.md, "入口ゲート「未検証」の
        # 実質素通り" fix, 2026-07-28).
        return [r for r in self.results if r.hard and (not r.passed or r.unverified)]

    @property
    def warnings(self) -> list[GateResult]:
        return [r for r in self.results
                if r.unverified or (not r.hard and not r.passed)]

    @property
    def ok(self) -> bool:
        return not self.hard_failures


# ---------------------------------------------------------------------------
# Screened-but-dropped candidates (missed-candidate tracking,
# docs/MASTER_OVERVIEW.ja.md §5.1) — every candidate the scout funnel drops
# past the surprise screen, recorded at drop time so the Phase-0 "BUY beats
# reject pile" comparison isn't limited to only the final tribunal-PASS
# stage. Recording at drop time (not re-fetching prices later) avoids
# survivorship bias: a delisted/acquired ticker can't be re-fetched after
# the fact, and is disproportionately likely to be the worst performer.
# ---------------------------------------------------------------------------

class ScreenedCandidateStage(str, Enum):
    ENRICHMENT_CAP = "enrichment_cap"    # sorted below scout_max_enrich, never enriched
    GATE_REJECT = "gate_reject"          # enriched (or enrichment itself failed), then rejected
    RANKING_CUTOFF = "ranking_cutoff"    # gate-passed, but outside this run's tribunal slot count


class ScreenedCandidate(BaseModel):
    id: str = Field(default_factory=lambda: new_id("scr"))
    recorded_at: datetime = Field(default_factory=utcnow)
    scan_id: int
    ticker: str
    event_date: date
    eps_surprise_pct: float
    revenue_surprise_pct: Optional[float] = None
    score: float
    score_version: str            # "full" (gap-aware) or "partial_no_gap"
    price: Optional[float] = None
    price_asof: Optional[date] = None
    stage: ScreenedCandidateStage
    rank: Optional[int] = None    # 1-indexed position among gate-passed candidates
    gate_report: Optional[GateReport] = None
    reject_reason: str = ""
    # What was visible at drop time. Enrichment already fetched these, and
    # they were then discarded for every dropped candidate — so a later drop
    # review had no way to reconstruct the qualitative picture the decision
    # was actually made against (docs/MASTER_OVERVIEW.ja.md §5.2(5)). Kept
    # at no extra API cost. Empty for the enrichment_cap stage, which is
    # dropped before any of this is fetched — absence here means "never
    # looked", not "looked and found nothing".
    news: list[NewsItem] = Field(default_factory=list)
    insider_activity: Optional[InsiderActivity] = None
    analyst_trend: Optional[AnalystTrend] = None


# ---------------------------------------------------------------------------
# Thesis (Bull output) — pre-registered, falsifiable
# ---------------------------------------------------------------------------

class EdgeType(str, Enum):
    UNDERREACTION = "underreaction"              # market slow to reprice (e.g. PEAD)
    OVERREACTION = "overreaction"                # market overshot; mean reversion
    STRUCTURAL_FLOW = "structural_flow"          # forced/mechanical buyers or sellers
    INFORMATION_SYNTHESIS = "information_synthesis"  # public dots not yet connected
    NONE_IDENTIFIED = "none_identified"


class Claim(BaseModel):
    """A falsifiable prediction with a probability and a deadline.

    Claims are the unit of accountability: at horizon they resolve TRUE or
    FALSE, feed Brier scoring, and drive the skill-vs-luck attribution.
    """
    id: str = Field(default_factory=lambda: new_id("clm"))
    statement: str
    probability: float = Field(ge=0.0, le=1.0)
    horizon_days: int
    verification: str  # how this will be checked at resolution time


class Scenario(BaseModel):
    name: str                      # bear / base / bull
    probability: float = Field(ge=0.0, le=1.0)
    price_target: float
    rationale: str = ""


class KillKind(str, Enum):
    PRICE_BELOW = "price_below"
    PRICE_ABOVE = "price_above"
    TIME_STOP_DAYS = "time_stop_days"
    EVENT = "event"                # requires human judgment (e.g. "guidance cut")


class KillCriterion(BaseModel):
    """Objective invalidation condition, defined BEFORE entry.

    The sentinel checks machine-checkable kinds daily; EVENT kinds are
    surfaced for human review. Once triggered, exit is the default and
    holding requires a written override — never the other way around.
    """
    kind: KillKind
    description: str
    level: Optional[float] = None  # for PRICE_* kinds
    days: Optional[int] = None     # for TIME_STOP_DAYS


class Thesis(BaseModel):
    summary: str
    edge_type: EdgeType
    edge_explanation: str          # why does this mispricing exist right now?
    other_side: str                # who is selling to us, and why are they wrong?
    claims: list[Claim]
    scenarios: list[Scenario]
    kill_criteria: list[KillCriterion]
    expected_holding_days: int


# ---------------------------------------------------------------------------
# Attack report (Adversary output)
# ---------------------------------------------------------------------------

class AttackCategory(str, Enum):
    THESIS_LOGIC = "thesis_logic"
    VALUATION = "valuation"
    CATALYST_DURABILITY = "catalyst_durability"
    CROWDING_POSITIONING = "crowding_positioning"
    LIQUIDITY = "liquidity"
    MACRO_REGIME = "macro_regime"
    DATA_INTEGRITY = "data_integrity"
    BASE_RATE = "base_rate"
    TIMING = "timing"
    GOVERNANCE_ACCOUNTING = "governance_accounting"


class Attack(BaseModel):
    id: str                             # stable, content-derived — set by
                                         # parse_attack_report(), never by the LLM
    category: AttackCategory
    severity: int = Field(ge=1, le=5)   # 5 = thesis-fatal if true
    statement: str
    evidence: str = ""
    is_kill_shot: bool = False


class AttackReport(BaseModel):
    attacks: list[Attack]
    strongest_short_case: str          # the best short thesis, written to win
    summary: str = ""

    @property
    def severe(self) -> list[Attack]:
        return [a for a in self.attacks if a.severity >= 4]


# ---------------------------------------------------------------------------
# Verdict (Judge output) and position plan (Risk Officer output)
# ---------------------------------------------------------------------------

class DecisionType(str, Enum):
    BUY = "buy"
    PASS = "pass"


class AddressedAttack(BaseModel):
    attack_id: str = ""       # matches Attack.id — "" only for pre-2026-07-28
                              # ledger rows recorded before this field existed
    attack_statement: str
    response: str
    converted_to_kill_criterion: bool = False


class Verdict(BaseModel):
    decision: DecisionType
    conviction: float = Field(ge=0.0, le=1.0)
    rationale: str
    addressed: list[AddressedAttack] = Field(default_factory=list)
    expected_value_pct: Optional[float] = None   # scenario-weighted return
    reward_risk: Optional[float] = None


class PositionPlan(BaseModel):
    nav: float
    risk_pct: float
    entry_ref_price: float
    stop_price: float
    target_price: float
    shares: int
    position_value: float
    position_pct_nav: float
    reward_risk: float
    expected_value_pct: float
    max_holding_days: int
    vetoes: list[str] = Field(default_factory=list)

    @property
    def approved(self) -> bool:
        return not self.vetoes


# ---------------------------------------------------------------------------
# Recommendation — the pre-registered record persisted to the ledger
# ---------------------------------------------------------------------------

class RecommendationStatus(str, Enum):
    SYSTEM_PASS = "system_pass"    # rejected by gates/tribunal/risk; never shown as BUY
    PROPOSED = "proposed"          # BUY delivered to the user, awaiting Yes/No
    DECLINED = "declined"          # user said No
    APPROVED = "approved"          # user said Yes, order not yet recorded
    OPEN = "open"                  # entry trade recorded
    CLOSED = "closed"              # exit trade recorded


class Recommendation(BaseModel):
    id: str = Field(default_factory=lambda: new_id("rec"))
    created_at: datetime = Field(default_factory=utcnow)
    ticker: str
    brief: CandidateBrief
    gate_report: GateReport
    thesis: Optional[Thesis] = None
    attack_report: Optional[AttackReport] = None
    verdict: Verdict
    plan: Optional[PositionPlan] = None
    model: str = ""                # LLM used, for reproducibility


# ---------------------------------------------------------------------------
# Post-trade: outcome and skill-vs-luck attribution
# ---------------------------------------------------------------------------

class OutcomeQuadrant(str, Enum):
    SKILL_WIN = "skill_win"          # thesis right, made money
    LUCKY_WIN = "lucky_win"          # thesis wrong, made money anyway
    UNLUCKY_LOSS = "unlucky_loss"    # thesis right, lost money
    DESERVED_LOSS = "deserved_loss"  # thesis wrong, lost money


class Outcome(BaseModel):
    recommendation_id: str
    entry_price: float
    exit_price: float
    entry_date: date
    exit_date: date
    pnl_pct: float
    holding_days: int
    thesis_accuracy: Optional[float] = None   # fraction of resolved claims true
    brier: Optional[float] = None             # calibration of claim probabilities
    quadrant: Optional[OutcomeQuadrant] = None
    notes: str = ""
