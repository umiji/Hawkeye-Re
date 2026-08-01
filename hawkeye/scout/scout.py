"""Scout — mechanical candidate discovery.

Funnel:  earnings calendar (all symbols)
           └─ surprise screen (EPS/revenue thresholds)       [cheap, no I/O]
                └─ enrichment (prices, profile; bounded)     [free-tier APIs]
                     └─ entry gates                          [doctrine]
                          └─ ranked shortlist → tribunal

Every stage's counts are recorded so the funnel itself is auditable:
"how many candidates did the screen see, and what fraction survived?"
is a first-class metric of the system, same as P&L.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import (
    CandidateBrief,
    Catalyst,
    CatalystType,
    GateReport,
    ScreenedCandidate,
    ScreenedCandidateStage,
    utc_date,
)
from hawkeye.gates.entry_gates import run_entry_gates
from hawkeye.marketdata.base import MarketDataProvider
from hawkeye.marketdata.snapshot import build_brief
from hawkeye.scout.earnings import (
    ScreenedEvent,
    parse_calendar,
    score_candidate,      # re-exported: the ranking score lives with the screen
    screen_events,
)


@dataclass
class ScoutCandidate:
    ticker: str
    event_date: date
    eps_surprise_pct: float
    revenue_surprise_pct: Optional[float]
    score: float = 0.0
    score_version: str = "partial_no_gap"  # "full" once gap_on_event_pct is known
    # How far the screen trusts its own numbers. An untrusted percentage is
    # still recorded — the drop review needs to know what the screen saw —
    # but it earns no score and is never handed to the tribunal as a fact.
    eps_surprise_trusted: bool = True
    revenue_surprise_trusted: bool = True
    conflicting_estimates: bool = False
    price: Optional[float] = None
    price_asof: Optional[date] = None
    brief: Optional[CandidateBrief] = None
    gate_report: Optional[GateReport] = None
    reject_reason: str = ""


@dataclass
class ScoutResult:
    scan_start: date
    scan_end: date
    scanned: int                 # raw calendar entries
    screened: int                # survived the surprise screen
    enriched: int                # fetched prices/profile for
    passed: list[ScoutCandidate] = field(default_factory=list)   # ranked
    rejected: list[ScoutCandidate] = field(default_factory=list)
    capped: list[ScoutCandidate] = field(default_factory=list)  # never enriched (scout_max_enrich)
    duplicates: int = 0          # already recorded by an earlier scan
    window_truncated: bool = False  # the lookback cap bounded the window

    def funnel(self) -> dict:
        return {"scanned": self.scanned, "screened": self.screened,
                "duplicates": self.duplicates,
                "enriched": self.enriched, "gate_passed": len(self.passed)}


# --- scan window (docs/MASTER_OVERVIEW.ja.md §5.2(1)) -----------------------

@dataclass(frozen=True)
class ScanWindow:
    """The range of earnings days one scan covers.

    Derived from the previous run rather than fixed, because runs are manual
    and irregular — no scheduler exists. A fixed narrow window would silently
    drop the earnings of every day the user didn't run, permanently; a fixed
    wide one re-reads the same days every time. `truncated` says the cap
    bounded the lookback, so earlier days went unscanned and it is known.
    """
    start: date
    end: date
    truncated: bool = False

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def previous_business_day(day: date) -> date:
    """The event-day close has to be final before a candidate can be ranked
    (the score rewards a confirming event-day move), and US earnings land
    pre-market or after the close — so today's reporters wait for the next
    run rather than being ranked on an unfinished session."""
    d = day - timedelta(days=1)
    while d.weekday() >= 5:      # Saturday/Sunday
        d -= timedelta(days=1)
    return d


def scan_window(today: date, last_scan_at: Optional[datetime],
                config: HawkeyeConfig,
                max_days: Optional[int] = None) -> ScanWindow:
    max_days = max_days if max_days is not None else config.scout_days_back
    end = previous_business_day(today)
    earliest = end - timedelta(days=max_days - 1)
    if last_scan_at is None:
        return ScanWindow(start=earliest, end=end)
    # One day of overlap with the previous run: the earnings calendar
    # back-fills and corrects entries after the fact, so a day read once is
    # worth reading again. Re-reading costs nothing — the (ticker, event
    # date) dedup drops anything already recorded.
    start = last_scan_at.date() - timedelta(days=1)
    truncated = start < earliest
    return ScanWindow(start=min(max(start, earliest), end), end=end,
                      truncated=truncated)


def _catalyst_description(screened: ScreenedEvent) -> str:
    """What the tribunal is told about the surprise, including what we do
    NOT stand behind. A number the screen distrusts must reach the Bull
    labelled, not silently — the prompts instruct both roles to trust
    structured surprise figures over prose, so an unlabelled bad number is
    worse than none at all."""
    parts = [f"EPS surprise {screened.eps_surprise_pct:+.1f}%"
             + ("" if screened.eps_surprise_trusted else
                " [UNVERIFIED: consensus too near zero for a percentage to"
                " carry information]")]
    if screened.revenue_surprise_pct is not None:
        parts.append(f"revenue surprise {screened.revenue_surprise_pct:+.1f}%"
                     + ("" if screened.revenue_surprise_trusted else
                        " [UNVERIFIED: actual and estimate are not on the same"
                        " accounting basis]"))
    if screened.event.conflicting_estimates:
        parts.append("the earnings calendar returned conflicting consensus"
                     " figures for this print; the most conservative was used")
    return ", ".join(parts) + " (guidance not machine-verified — check news)"


def _candidate_from(screened: ScreenedEvent,
                    reject_reason: str = "") -> ScoutCandidate:
    """A ScoutCandidate carrying both what the screen measured and how far it
    trusts the measurement. Scoring uses only the trusted values."""
    rev = screened.revenue_surprise_pct
    return ScoutCandidate(
        ticker=screened.event.ticker, event_date=screened.event.day,
        eps_surprise_pct=round(screened.eps_surprise_pct, 1),
        revenue_surprise_pct=round(rev, 1) if rev is not None else None,
        score=score_candidate(screened.scored_eps_pct,
                              screened.scored_revenue_pct, None),
        eps_surprise_trusted=screened.eps_surprise_trusted,
        revenue_surprise_trusted=screened.revenue_surprise_trusted,
        conflicting_estimates=screened.event.conflicting_estimates,
        reject_reason=reject_reason)


def run_scout(calendar_source, provider: MarketDataProvider, config: HawkeyeConfig,
              days_back: Optional[int] = None,
              today: Optional[date] = None,
              window: Optional[ScanWindow] = None,
              already_seen: Optional[set[tuple[str, date]]] = None) -> ScoutResult:
    """calendar_source: object with earnings_calendar(start, end) -> list[dict]
    provider: MarketDataProvider for enrichment (prices/profile/news).
    window: the earnings days to cover — normally built by scan_window()
        from the previous run. `days_back` is the manual override.
    already_seen: (ticker, event date) pairs an earlier scan already
        recorded, dropped before enrichment so they cost no API calls.
    """
    today = today or date.today()
    if window is None:
        end = previous_business_day(today)
        span = days_back if days_back is not None else config.scout_days_back
        window = ScanWindow(start=end - timedelta(days=span - 1), end=end)

    raw = calendar_source.earnings_calendar(window.start, window.end)
    events = parse_calendar(raw)
    screened = screen_events(events,
                             config.scout_min_eps_surprise_pct,
                             config.scout_min_revenue_surprise_pct,
                             config.scout_min_abs_eps_estimate,
                             config.scout_max_trusted_revenue_surprise_pct)

    # Deduplicate before enrichment: windows overlap by design, so the same
    # earnings event recurs across runs. Re-evaluating it would both waste
    # free-tier calls and double-count it in the drop statistics.
    seen = already_seen or set()
    fresh = [s for s in screened if (s.event.ticker, s.event.day) not in seen]
    duplicates = len(screened) - len(fresh)
    screened_total, screened = len(screened), fresh

    to_enrich = screened[:config.scout_max_enrich]
    passed: list[ScoutCandidate] = []
    rejected: list[ScoutCandidate] = []

    for s in to_enrich:
        event = s.event
        candidate = _candidate_from(s)   # score refined below once gap is known
        catalyst = Catalyst(
            type=CatalystType.EARNINGS_BEAT,
            description=_catalyst_description(s),
            event_date=event.day, source="scout/finnhub-earnings-calendar")
        try:
            # Only trusted figures become structured snapshot fields: the
            # tribunal prompts tell both roles to prefer these over prose, so
            # a distrusted number here would be laundered into a fact. The
            # catalyst description still says what was measured, and why it
            # is not stood behind.
            brief = build_brief(
                candidate.ticker, catalyst, provider,
                overrides={
                    "eps_surprise_pct": (
                        round(s.scored_eps_pct, 1)
                        if s.scored_eps_pct is not None else None),
                    "revenue_surprise_pct": (
                        round(s.scored_revenue_pct, 1)
                        if s.scored_revenue_pct is not None else None)},
                config=config)
        except Exception as exc:  # enrichment failure = rejection, visibly
            candidate.reject_reason = f"enrichment failed: {exc}"
            rejected.append(candidate)
            continue
        candidate.brief = brief
        candidate.price = brief.snapshot.price
        candidate.price_asof = utc_date(brief.snapshot.as_of)
        candidate.score = score_candidate(
            s.scored_eps_pct, s.scored_revenue_pct,
            brief.snapshot.gap_on_event_pct)
        candidate.score_version = "full"
        candidate.gate_report = run_entry_gates(brief.snapshot, catalyst,
                                                config, today=today)
        if not candidate.gate_report.ok:
            candidate.reject_reason = "gate: " + ", ".join(
                g.name for g in candidate.gate_report.hard_failures)
            rejected.append(candidate)
            continue
        passed.append(candidate)

    passed.sort(key=lambda c: -c.score)

    # #2 in docs/MASTER_OVERVIEW.ja.md §5.1: candidates sorted below
    # scout_max_enrich never get a full brief — one cheap price-only fetch
    # each (not the full multi-call enrichment) so they're still trackable,
    # instead of vanishing with no record at all.
    capped: list[ScoutCandidate] = []
    for s in screened[config.scout_max_enrich:]:
        c = _candidate_from(
            s, reject_reason=("enrichment cap: outside top scout_max_enrich"
                              " by score"))
        try:
            bars = provider.daily_history(s.event.ticker, days=5)
            if bars:
                c.price, c.price_asof = bars[-1].close, bars[-1].day
        except Exception:
            pass
        capped.append(c)

    return ScoutResult(scan_start=window.start, scan_end=window.end,
                       scanned=len(raw), screened=screened_total,
                       enriched=len(to_enrich),
                       passed=passed, rejected=rejected, capped=capped,
                       duplicates=duplicates,
                       window_truncated=window.truncated)


def _visible_at_drop(c: ScoutCandidate) -> dict:
    """The qualitative data enrichment already fetched for this candidate.

    Empty when the candidate never got a brief — dropped at the enrichment
    cap, or enrichment itself failed. Absence is therefore "never looked",
    which is a different fact from "looked and found nothing"; the stage
    field is what distinguishes them (docs/MASTER_OVERVIEW.ja.md §5.2(5)).
    """
    if c.brief is None:
        return {}
    return {"news": c.brief.news,
            "insider_activity": c.brief.insider_activity,
            "analyst_trend": c.brief.analyst_trend}


def _measured(c: ScoutCandidate) -> dict:
    """What the screen measured and how far it trusted it — the fields every
    dropped-candidate record carries regardless of which stage dropped it."""
    return {"eps_surprise_pct": c.eps_surprise_pct,
            "revenue_surprise_pct": c.revenue_surprise_pct,
            "eps_surprise_trusted": c.eps_surprise_trusted,
            "revenue_surprise_trusted": c.revenue_surprise_trusted,
            "conflicting_estimates": c.conflicting_estimates,
            "score": c.score, "score_version": c.score_version,
            "price": c.price, "price_asof": c.price_asof}


def build_screened_candidates(
    result: ScoutResult, scan_id: int, sent_to_tribunal_n: int = 0,
) -> list[ScreenedCandidate]:
    """Convert everything run_scout() dropped (docs/MASTER_OVERVIEW.ja.md
    §5.1, stages #2-#4) into persistable records for
    Ledger.record_screened_candidates(). `sent_to_tribunal_n` is the one
    piece run_scout() itself can't know — how many of `result.passed` this
    particular run forwarded on (the caller's --evaluate/--open-cases
    choice) — so the ranking-cutoff tier (#4) is computed here, by the
    caller, immediately after that choice is made."""
    out: list[ScreenedCandidate] = []
    for c in result.capped:
        out.append(ScreenedCandidate(
            scan_id=scan_id, ticker=c.ticker, event_date=c.event_date,
            stage=ScreenedCandidateStage.ENRICHMENT_CAP,
            reject_reason=c.reject_reason,
            **_measured(c), **_visible_at_drop(c)))
    for c in result.rejected:
        out.append(ScreenedCandidate(
            scan_id=scan_id, ticker=c.ticker, event_date=c.event_date,
            stage=ScreenedCandidateStage.GATE_REJECT,
            gate_report=c.gate_report, reject_reason=c.reject_reason,
            **_measured(c), **_visible_at_drop(c)))
    for i, c in enumerate(result.passed[sent_to_tribunal_n:],
                          start=sent_to_tribunal_n + 1):
        out.append(ScreenedCandidate(
            scan_id=scan_id, ticker=c.ticker, event_date=c.event_date,
            stage=ScreenedCandidateStage.RANKING_CUTOFF, rank=i,
            gate_report=c.gate_report,
            reject_reason=f"rank {i}, outside this run's top {sent_to_tribunal_n}",
            **_measured(c), **_visible_at_drop(c)))
    return out
