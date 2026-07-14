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
from datetime import date, timedelta
from typing import Optional

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import (
    CandidateBrief,
    Catalyst,
    CatalystType,
    GateReport,
)
from hawkeye.gates.entry_gates import run_entry_gates
from hawkeye.marketdata.snapshot import build_brief
from hawkeye.scout.earnings import parse_calendar, screen_events


@dataclass
class ScoutCandidate:
    ticker: str
    event_date: date
    eps_surprise_pct: float
    revenue_surprise_pct: Optional[float]
    score: float = 0.0
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

    def funnel(self) -> dict:
        return {"scanned": self.scanned, "screened": self.screened,
                "enriched": self.enriched, "gate_passed": len(self.passed)}


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


def run_scout(calendar_source, provider, config: HawkeyeConfig,
              days_back: Optional[int] = None,
              today: Optional[date] = None) -> ScoutResult:
    """calendar_source: object with earnings_calendar(start, end) -> list[dict]
    provider: MarketDataProvider for enrichment (prices/profile/news).
    """
    today = today or date.today()
    days_back = days_back or config.scout_days_back
    start = today - timedelta(days=days_back)

    raw = calendar_source.earnings_calendar(start, today)
    events = parse_calendar(raw)
    screened = screen_events(events,
                             config.scout_min_eps_surprise_pct,
                             config.scout_min_revenue_surprise_pct)

    to_enrich = screened[:config.scout_max_enrich]
    passed: list[ScoutCandidate] = []
    rejected: list[ScoutCandidate] = []

    for event, eps_s, rev_s in to_enrich:
        candidate = ScoutCandidate(
            ticker=event.ticker, event_date=event.day,
            eps_surprise_pct=round(eps_s, 1),
            revenue_surprise_pct=round(rev_s, 1) if rev_s is not None else None)
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
                                                   if rev_s is not None else None)})
        except Exception as exc:  # enrichment failure = rejection, visibly
            candidate.reject_reason = f"enrichment failed: {exc}"
            rejected.append(candidate)
            continue
        candidate.brief = brief
        candidate.gate_report = run_entry_gates(brief.snapshot, catalyst,
                                                config, today=today)
        if not candidate.gate_report.ok:
            candidate.reject_reason = "gate: " + ", ".join(
                g.name for g in candidate.gate_report.hard_failures)
            rejected.append(candidate)
            continue
        candidate.score = score_candidate(
            eps_s, rev_s, brief.snapshot.gap_on_event_pct)
        passed.append(candidate)

    passed.sort(key=lambda c: -c.score)
    return ScoutResult(scan_start=start, scan_end=today,
                       scanned=len(raw), screened=len(screened),
                       enriched=len(to_enrich),
                       passed=passed, rejected=rejected)
