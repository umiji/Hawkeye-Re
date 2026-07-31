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
from hawkeye.scout.earnings import parse_calendar, screen_events


@dataclass
class ScoutCandidate:
    ticker: str
    event_date: date
    eps_surprise_pct: float
    revenue_surprise_pct: Optional[float]
    score: float = 0.0
    score_version: str = "partial_no_gap"  # "full" once gap_on_event_pct is known
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


def score_candidate(eps_surprise: float, revenue_surprise: Optional[float],
                    gap_on_event_pct: Optional[float]) -> float:
    """Deterministic ranking score.

    Rewards surprise magnitude, and an event-day reaction that CONFIRMS the
    surprise without exhausting it (the classic drift setup): a modest
    positive gap beats both a negative reaction (market disagrees with the
    print — respect that) and a huge gap (repricing likely complete).
    """
    score = min(eps_surprise, 50.0)
    if revenue_surprise is not None and revenue_surprise > 0:
        score += min(revenue_surprise * 2.0, 20.0)
    if gap_on_event_pct is not None:
        if 2.0 <= gap_on_event_pct <= 15.0:
            score += 15.0
        elif 0.0 <= gap_on_event_pct < 2.0:
            score += 5.0
        elif gap_on_event_pct < 0.0 or gap_on_event_pct > 25.0:
            score -= 10.0
    return round(score, 2)


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
                             config.scout_min_revenue_surprise_pct)

    # Deduplicate before enrichment: windows overlap by design, so the same
    # earnings event recurs across runs. Re-evaluating it would both waste
    # free-tier calls and double-count it in the drop statistics.
    seen = already_seen or set()
    fresh = [row for row in screened if (row[0].ticker, row[0].day) not in seen]
    duplicates = len(screened) - len(fresh)
    screened_total, screened = len(screened), fresh

    to_enrich = screened[:config.scout_max_enrich]
    passed: list[ScoutCandidate] = []
    rejected: list[ScoutCandidate] = []

    for event, eps_s, rev_s in to_enrich:
        candidate = ScoutCandidate(
            ticker=event.ticker, event_date=event.day,
            eps_surprise_pct=round(eps_s, 1),
            revenue_surprise_pct=round(rev_s, 1) if rev_s is not None else None,
            score=score_candidate(eps_s, rev_s, None))  # refined below if enrichment succeeds
        catalyst = Catalyst(
            type=CatalystType.EARNINGS_BEAT,
            description=(f"EPS surprise {eps_s:+.1f}%"
                         + (f", revenue surprise {rev_s:+.1f}%"
                            if rev_s is not None else "")
                         + " (guidance not machine-verified — check news)"),
            event_date=event.day, source="scout/finnhub-earnings-calendar")
        try:
            brief = build_brief(
                candidate.ticker, catalyst, provider,
                overrides={"eps_surprise_pct": round(eps_s, 1),
                          "revenue_surprise_pct": (round(rev_s, 1)
                                                   if rev_s is not None else None)},
                config=config)
        except Exception as exc:  # enrichment failure = rejection, visibly
            candidate.reject_reason = f"enrichment failed: {exc}"
            rejected.append(candidate)
            continue
        candidate.brief = brief
        candidate.price = brief.snapshot.price
        candidate.price_asof = utc_date(brief.snapshot.as_of)
        candidate.score = score_candidate(
            eps_s, rev_s, brief.snapshot.gap_on_event_pct)
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
    for event, eps_s, rev_s in screened[config.scout_max_enrich:]:
        c = ScoutCandidate(
            ticker=event.ticker, event_date=event.day,
            eps_surprise_pct=round(eps_s, 1),
            revenue_surprise_pct=round(rev_s, 1) if rev_s is not None else None,
            score=score_candidate(eps_s, rev_s, None),
            reject_reason="enrichment cap: outside top scout_max_enrich by EPS surprise")
        try:
            bars = provider.daily_history(event.ticker, days=5)
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
            eps_surprise_pct=c.eps_surprise_pct,
            revenue_surprise_pct=c.revenue_surprise_pct,
            score=c.score, score_version=c.score_version,
            price=c.price, price_asof=c.price_asof,
            stage=ScreenedCandidateStage.ENRICHMENT_CAP,
            reject_reason=c.reject_reason,
            **_visible_at_drop(c)))
    for c in result.rejected:
        out.append(ScreenedCandidate(
            scan_id=scan_id, ticker=c.ticker, event_date=c.event_date,
            eps_surprise_pct=c.eps_surprise_pct,
            revenue_surprise_pct=c.revenue_surprise_pct,
            score=c.score, score_version=c.score_version,
            price=c.price, price_asof=c.price_asof,
            stage=ScreenedCandidateStage.GATE_REJECT,
            gate_report=c.gate_report, reject_reason=c.reject_reason,
            **_visible_at_drop(c)))
    for i, c in enumerate(result.passed[sent_to_tribunal_n:],
                          start=sent_to_tribunal_n + 1):
        out.append(ScreenedCandidate(
            scan_id=scan_id, ticker=c.ticker, event_date=c.event_date,
            eps_surprise_pct=c.eps_surprise_pct,
            revenue_surprise_pct=c.revenue_surprise_pct,
            score=c.score, score_version=c.score_version,
            price=c.price, price_asof=c.price_asof,
            stage=ScreenedCandidateStage.RANKING_CUTOFF, rank=i,
            gate_report=c.gate_report,
            reject_reason=f"rank {i}, outside this run's top {sent_to_tribunal_n}",
            **_visible_at_drop(c)))
    return out
