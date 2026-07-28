"""Cohort benchmark — does the funnel actually add value?

Every evaluated candidate is stored with its evaluation-time price, whether
we bought it or not. Comparing forward returns of the cohorts answers the
question that P&L alone cannot:

    BUY cohort  >  tribunal-PASS cohort  >  (ideally) gate-reject cohort

If BUYs don't beat the candidates we rejected, the screening/judgment logic
is not adding value — regardless of whether the book is up. This is the
primary viability metric for Phase 0 (docs/ROADMAP.md).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from hawkeye.contracts.models import (
    DecisionType,
    Recommendation,
    RecommendationStatus,
)
from hawkeye.marketdata.base import Bar


def forward_return(bars: list[Bar], start_day: date,
                   horizon_days: int) -> Optional[float]:
    """Return % from the last close on/before start_day to the first close
    on/after start_day + horizon_days. None if either side is missing."""
    base = next((b for b in reversed(bars) if b.day <= start_day), None)
    target_day = start_day + timedelta(days=horizon_days)
    end = next((b for b in bars if b.day >= target_day), None)
    if base is None or end is None or base.close <= 0:
        return None
    return (end.close / base.close - 1.0) * 100.0


def cohort_of(rec: Recommendation) -> str:
    if rec.verdict.decision == DecisionType.BUY:
        return "BUY"
    if rec.thesis is None:
        return "GATE_REJECT"
    return "TRIBUNAL_PASS"


def collect_samples(
    records: list[Recommendation],
    provider,
    today: date,
    horizon_days: int,
    source: str = "scout",
) -> tuple[list[tuple[str, float]], int, dict[str, int]]:
    """Turn ledger records into (cohort, forward_return) samples for
    cohort_stats(), plus the two ways a record can fail to become a sample:

    - `pending`: the horizon hasn't elapsed yet. Expected, not a bug.
    - `censored` (per cohort): the price history fetch failed or returned no
      usable bars (delisting, ticker change, acquisition, API outage). A
      silently-dropped ticker is disproportionately likely to be the worst
      performer in its cohort, so treating this the same as `pending` would
      hide survivorship bias in the comparison instead of surfacing it.

    `source` restricts which cohort of records is included: "scout" (the
    default — per docs/ROADMAP.md, manually-picked `evaluate` candidates are
    a separate cohort and must never be mixed into viability stats),
    "manual", or "all".
    """
    samples: list[tuple[str, float]] = []
    pending = 0
    censored = {"BUY": 0, "TRIBUNAL_PASS": 0, "GATE_REJECT": 0}
    for rec in records:
        is_scout = rec.brief.catalyst.source.startswith("scout")
        if source == "scout" and not is_scout:
            continue
        if source == "manual" and is_scout:
            continue
        cohort = cohort_of(rec)
        eval_day = rec.created_at.date()
        if (today - eval_day).days < horizon_days:
            pending += 1
            continue
        try:
            bars = provider.daily_history(rec.ticker, days=400)
        except Exception:
            censored[cohort] += 1
            continue
        ret = forward_return(bars, eval_day, horizon_days)
        if ret is None:
            censored[cohort] += 1
            continue
        samples.append((cohort, ret))
    return samples, pending, censored


def reason_snippet(rec: Recommendation, status: str, max_len: int = 160) -> str:
    """One-line reason a candidate was NOT bought, for individual postmortem
    review (as opposed to cohort_stats' aggregate view)."""
    if status == RecommendationStatus.DECLINED.value:
        return "ユーザーが見送りを選択(システムはBUYを提案していた)"
    text = rec.verdict.rationale.strip().splitlines()[0] if rec.verdict.rationale else ""
    return text[:max_len]


def cohort_stats(samples: list[tuple[str, float]]) -> dict[str, dict]:
    """samples: (cohort, forward_return_pct) -> per-cohort summary."""
    out: dict[str, dict] = {}
    for cohort in ("BUY", "TRIBUNAL_PASS", "GATE_REJECT"):
        returns = sorted(r for c, r in samples if c == cohort)
        if not returns:
            out[cohort] = {"n": 0, "mean": None, "median": None,
                           "win_rate": None}
            continue
        n = len(returns)
        mid = n // 2
        median = returns[mid] if n % 2 else (returns[mid - 1] + returns[mid]) / 2
        out[cohort] = {
            "n": n,
            "mean": round(sum(returns) / n, 2),
            "median": round(median, 2),
            "win_rate": round(sum(1 for r in returns if r > 0) / n, 2),
        }
    return out
