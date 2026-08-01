"""Core data contracts shared across every Hawkeye service.

These models are the ONLY interface between services (scout, tribunal,
risk, ledger, sentinel, reports). Services never import each other's
internals. Anything persisted to the ledger is one of these models
serialized to JSON, which is what makes the record replayable and
auditable years later.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Literal, Optional

import hashlib

from pydantic import BaseModel, Field, model_validator

JST = timezone(timedelta(hours=9), "JST")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> datetime:
    """The moment a record is created, in the reader's own timezone (JST).

    Records are stamped the way the user reads them, so the ledger is legible
    without mental arithmetic. The `+09:00` offset is part of every stored
    string, so each timestamp is still an unambiguous instant and rows
    written before this change (which carry `+00:00`) stay directly
    comparable — see `ledger.store._instant`, which is why ordering can no
    longer be left to SQLite's text comparison.
    """
    return datetime.now(JST)


def to_jst(value: datetime) -> datetime:
    """Same instant, expressed in JST. A value with no offset at all is read
    as UTC — that is what the pre-2026-07-31 records meant."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(JST)


def utc_date(value: datetime) -> date:
    """The UTC calendar date of an instant.

    Deliberately NOT the JST date: this is the anchor every forward-return
    measurement counts trading days from, and it must keep meaning what it
    meant before timestamps became JST. A JST-dated anchor would push the
    holding period one day later for anything recorded after 15:00 UTC,
    silently changing every cohort comparison in the ledger.
    """
    if value.tzinfo is None:
        return value.date()
    return value.astimezone(timezone.utc).date()


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
    as_of: datetime = Field(default_factory=now)
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
    recorded_at: datetime = Field(default_factory=now)
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
# Drop-candidate review (docs/MASTER_OVERVIEW.ja.md §5.2(3))
# ---------------------------------------------------------------------------

class MissCategory(str, Enum):
    """Why a dropped candidate moved the way it did.

    A join key, not prose: free text never forms the piles §5.2(3) [4] acts
    on. `UNFORESEEABLE` and `GATE_CORRECT` are load-bearing — without a place
    to record "nothing to fix here" and "the gate was right", only misses
    accumulate and every review round tilts one way: loosen the gate.

    `COLLECTION_GAP` exists because `UNFORESEEABLE` was doing two jobs
    (2026-08-01). "The information that moved it was published before we
    decided, and we simply never collected it" is not unforeseeable — it is
    the most fixable defect we have, and it was hiding inside the one
    category that ends an inquiry. The two are now split by a checkable
    fact: whether the article predates the decision.
    """
    GATE_THRESHOLD_TOO_STRICT = "gate_threshold_too_strict"
    SCORE_FORMULA_WRONG = "score_formula_wrong"    # ranked low, actually strong
    ENRICHMENT_CAP = "enrichment_cap"              # dropped before being looked at
    DATA_GAP = "data_gap"                          # unverified, not a threshold problem
    COLLECTION_GAP = "collection_gap"              # was public in time; we missed it
    UNFORESEEABLE = "unforeseeable"                # arose AFTER the call; NOT fixable
    GATE_CORRECT = "gate_correct"                  # fell — evidence the gate works
    OTHER = "other"                                # requires notes; >3 means re-cut


class ProposedChange(BaseModel):
    """A revision the review argues for. Split into fields so [4] can group
    proposals that point at the same knob instead of re-reading paragraphs."""
    target: str        # config key or prompt section to change
    direction: str     # which way, in the target's own units
    rationale: str = ""


class DropReview(BaseModel):
    """One dropped candidate, scored at one fixed checkpoint.

    Separate from `ScreenedCandidate` on purpose: that record is what was
    true when the decision was made, this is what happened afterwards.
    Folding the second into the first would rewrite a decision record — the
    same failure invariant 1 forbids for recommendation payloads.

    The reproduction inputs (prices, benchmark return, beta, ATR) are stored
    with the verdict because they cannot be recovered later: beta comes from
    a rolling 250-day regression, and splits/dividends retroactively rewrite
    the prices it was estimated from. Alpha and z alone are unauditable.
    """
    id: str = Field(default_factory=lambda: new_id("drv"))
    schema_version: int = 1

    # -- identity: which decision is being reviewed
    screened_candidate_id: Optional[str] = None
    scan_id: Optional[int] = None
    rec_id: Optional[str] = None       # set when the candidate reached the tribunal
    ticker: str
    cohort: str                        # funnel stage, see scout.drop_review.COHORTS

    # -- timing
    reviewed_at: datetime = Field(default_factory=now)
    checkpoint: Literal["t5", "t10"]   # fixed by design; never re-checked after
    checkpoint_date: Optional[date] = None
    decision_date: date
    horizon_days: int

    # -- reproduction inputs
    price_at_decision: Optional[float] = None
    price_at_checkpoint: Optional[float] = None
    raw_return_pct: float
    benchmark_return_pct: Optional[float] = None
    beta: Optional[float] = None
    beta_window: int
    atr_pct: Optional[float] = None

    # -- verdict
    alpha_pct: float
    z: float
    direction: Literal["up", "down"]

    # -- analysis (§5.2(3) [3]; empty until the investigation runs)
    what_happened: str = ""
    # Quotes from the record as it stood at decision time. The anti-hindsight
    # constraint: an explanation that cites nothing visible then is a story.
    visible_evidence: list[str] = Field(default_factory=list)
    miss_category: Optional[MissCategory] = None
    notes: str = ""
    evidence_urls: list[str] = Field(default_factory=list)

    # -- proposed revision (§5.2(3) [4])
    proposed_change: Optional[ProposedChange] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    # -- provenance
    reviewer_model: str = ""

    @model_validator(mode="after")
    def _check(self) -> "DropReview":
        if not (self.screened_candidate_id or self.rec_id):
            raise ValueError(
                "a drop review must reference the screened candidate or the "
                "recommendation it reviews")
        if self.miss_category is MissCategory.OTHER and not self.notes.strip():
            raise ValueError("miss_category 'other' requires notes")
        return self


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


def attack_content_id(category, statement: str, evidence: str = "") -> str:
    """Deterministic id for an attack, derived from its content.

    Content-hashing rather than uuid4 means two independent parses of the
    same attack agree on the same id — which is what lets the Judge cite an
    attack it can only see rendered, and lets `_judge_rule_check` match by id
    instead of by fragile substring comparison on the statement text.
    """
    digest = hashlib.sha256(
        "|".join((str(category), statement, evidence)).encode("utf-8")
    ).hexdigest()[:12]
    return f"atk_{digest}"


class Attack(BaseModel):
    id: str                             # stable, content-derived — set by
                                         # parse_attack_report(), never by the LLM
    category: AttackCategory
    severity: int = Field(ge=1, le=5)   # 5 = thesis-fatal if true
    statement: str
    evidence: str = ""
    is_kill_shot: bool = False

    @model_validator(mode="before")
    @classmethod
    def _fill_legacy_id(cls, data):
        """Recover ids for ledger rows written before `id` existed.

        Attack ids were added on 2026-07-28; records stored before that have
        none, and a required field made those recommendations permanently
        unloadable — `hawkeye show` and every cross-record analysis simply
        crashed on them. The id is a pure function of the attack's content,
        so it can be recomputed exactly rather than invented, and the stored
        payload is never rewritten (invariant 1: pre-registered records are
        immutable; this fills the field on the way *in*, not on disk).
        """
        if isinstance(data, dict) and not data.get("id"):
            statement = data.get("statement")
            if isinstance(statement, str):
                return {**data, "id": attack_content_id(
                    data.get("category", ""), statement,
                    data.get("evidence", "") or "")}
        return data


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
    created_at: datetime = Field(default_factory=now)
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
