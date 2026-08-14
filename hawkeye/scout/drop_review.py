"""Drop-candidate review — was the screen right to drop it?

docs/design/MASTER_OVERVIEW.ja.md §5.2(3) (which supersedes the earlier, coarser
§5.1 [2] sketch). The ledger records every candidate the funnel dropped, but
a record nobody scores teaches nothing. This module is the scoring half:
measure each dropped candidate at two fixed checkpoints and attribute the
outliers back to the exact stage — and, for gate rejects, the exact gate —
that dropped them.

The point is not "did we miss a winner" (some misses are correct). It is
"is one of our own screening rules systematically costing us money", which
only shows up as a *group* beating its own comparison basis.

Three things are fixed by design, not chosen per run:

- **Checkpoints are T+5 and T+10 trading days, and nothing after.** Looking
  out a month or two buries the catalyst's effect under unrelated noise; the
  thesis being scored is a short-horizon earnings-surprise drift, so the
  measurement window has to match it. "Re-check later and see" is how a
  disappointing result gets re-run until it flatters.
- **Beta is regressed over the trailing 250 trading days, ending at the
  decision day.** Estimating it from the window being measured would let the
  yardstick absorb the very move it is supposed to measure.
- **The outlier bar is |z| >= 1.5, both directions.** Counting only the
  candidates that rose and then loosening the gate that dropped them is a
  guaranteed way to make performance worse; the falls are what tell you the
  gate was doing its job.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date
from typing import Iterable, Optional

from hawkeye.contracts.models import (
    DecisionType,
    DropReview,
    Recommendation,
    ScreenedCandidate,
    ScreenedCandidateStage,
    utc_date,
)
from hawkeye.marketdata.base import Bar
from hawkeye.scout.benchmark import (
    forward_return,
    min_calendar_days_for_trading_days,
)

# Fixed observation points, in trading days (§5.2(3): "T+5営業日 → T+10営業日
# で確定、以降は再チェックしない").
CHECKPOINT_TRADING_DAYS: dict[str, int] = {"t5": 5, "t10": 10}

# Beta regression window, in trading days (§5.2(3) 第1段).
BETA_WINDOW_TRADING_DAYS = 250
# Minimum overlapping days before a beta is trustworthy enough to publish.
BETA_MIN_DAYS = 60

# Outlier bar, both directions (§5.2(3) 第2段).
Z_THRESHOLD = 1.5

# Ordered widest-funnel-last so reports read down the funnel.
COHORTS: tuple[str, ...] = (
    "BUY",             # adopted
    "TRIBUNAL_PASS",   # reached the tribunal, judged PASS
    "RANKING_CUTOFF",  # gate-passed, ranked below this run's tribunal slots
    "GATE_REJECT",     # rejected by an entry gate
    "ENRICHMENT_CAP",  # never enriched: sorted below scout_max_enrich
    "ACTUAL_TIMEOUT",  # the print's own numbers never arrived; given up on
)

# `actual_pending` is deliberately absent, and is filtered out before a
# candidate ever becomes a TrackedCandidate. It is not a drop: the print was
# never judged, it is still open, and one row exists per look — so counting it
# would both measure a decision nobody made and count one print many times.
# `actual_timeout` IS counted: giving up because the data never came is a real
# outcome, and "how often does that cost us a good name" is exactly the kind
# of thing the drop review exists to measure.

# Cohorts worth investigating one name at a time. Enrichment-cap drops are
# deliberately excluded: their only recorded reason is "ranked 16th or lower
# by score", so reading an individual name cannot tell you what to
# change — the only two levers are `scout_max_enrich` and the sort key, and
# those move on the *group* mean, not on any one story (§5.2(6)). They stay
# in every aggregate for exactly that reason; it is the per-name reading
# that would be wasted effort.
INVESTIGATION_COHORTS: tuple[str, ...] = tuple(
    c for c in COHORTS if c != "ENRICHMENT_CAP")

_STAGE_TO_COHORT = {
    ScreenedCandidateStage.ENRICHMENT_CAP: "ENRICHMENT_CAP",
    ScreenedCandidateStage.GATE_REJECT: "GATE_REJECT",
    ScreenedCandidateStage.RANKING_CUTOFF: "RANKING_CUTOFF",
    ScreenedCandidateStage.ACTUAL_TIMEOUT: "ACTUAL_TIMEOUT",
}


def is_reviewable(c: ScreenedCandidate) -> bool:
    """Whether this dropped row represents a decision worth measuring.

    False for a print still being held: nothing was judged, the print is still
    open, and there is one row per look. Both counting it as a drop and
    counting it repeatedly would corrupt the tallies the screen is revised on.
    """
    return c.stage in _STAGE_TO_COHORT


@dataclass(frozen=True)
class TrackedCandidate:
    """One candidate to follow, normalized across the two ledger tables that
    hold them (`screened_candidates` and `recommendations`)."""
    ticker: str
    cohort: str
    scan_id: Optional[int]
    decision_date: date
    screened_candidate_id: Optional[str] = None
    rec_id: Optional[str] = None
    reject_reason: str = ""
    failed_gates: tuple[str, ...] = ()
    score: float = 0.0


@dataclass(frozen=True)
class CheckpointResult:
    """One candidate measured at one checkpoint.

    Every input the verdict was derived from is carried alongside the verdict
    (§5.2(3) "再現用の入力値を必ず保存します"): beta is re-estimated from a
    rolling window, so re-running this months later produces a different
    number, and alpha/z alone would leave no way to audit the original call.
    """
    ticker: str
    cohort: str
    scan_id: Optional[int]
    decision_date: date
    checkpoint: str                  # "t5" | "t10"
    checkpoint_date: Optional[date]
    horizon_days: int
    price_at_decision: Optional[float]
    price_at_checkpoint: Optional[float]
    raw_return_pct: float
    benchmark_return_pct: Optional[float]
    beta: Optional[float]
    beta_window: int
    atr_pct: Optional[float]
    # None when beta, the benchmark return, or ATR could not be computed.
    # Left None rather than falling back to the raw return, which would
    # silently relabel pure market beta as selection skill.
    alpha_pct: Optional[float]
    z: Optional[float]
    direction: Optional[str]         # "up" | "down"
    screened_candidate_id: Optional[str] = None
    rec_id: Optional[str] = None
    reject_reason: str = ""
    failed_gates: tuple[str, ...] = ()
    score: float = 0.0
    peer_baseline_pct: Optional[float] = None
    peer_excess_pct: Optional[float] = None

    @property
    def is_outlier(self) -> bool:
        return self.z is not None and abs(self.z) >= Z_THRESHOLD


# --- adapters --------------------------------------------------------------

def failed_gate_names(gate_report) -> tuple[str, ...]:
    """Names of the gates that blocked a candidate. An unverified gate is
    named too (suffixed): invariant 6 treats missing data on a hard gate as a
    failure, so a candidate dropped on absent data must stay attributable to
    the gate whose data was absent instead of vanishing from the tally."""
    if gate_report is None:
        return ()
    names: list[str] = []
    for g in getattr(gate_report, "results", []):
        if not g.passed:
            names.append(g.name)
        elif getattr(g, "unverified", False):
            names.append(f"{g.name}:unverified")
    return tuple(names)


def from_screened(c: ScreenedCandidate) -> TrackedCandidate:
    return TrackedCandidate(
        ticker=c.ticker,
        cohort=_STAGE_TO_COHORT[c.stage],
        scan_id=c.scan_id,
        # The drop decision was made against this price, so returns must be
        # measured from the same day — not from `recorded_at`, which can sit
        # a day later now that the scan window ends on the previous business
        # day.
        decision_date=c.price_asof or utc_date(c.recorded_at),
        screened_candidate_id=c.id,
        reject_reason=c.reject_reason,
        failed_gates=failed_gate_names(c.gate_report),
        score=c.score)


def from_recommendation(rec: Recommendation) -> TrackedCandidate:
    if rec.verdict.decision == DecisionType.BUY:
        cohort = "BUY"
    elif rec.thesis is None:
        cohort = "GATE_REJECT"
    else:
        cohort = "TRIBUNAL_PASS"
    rationale = (rec.verdict.rationale or "").strip()
    return TrackedCandidate(
        ticker=rec.ticker, cohort=cohort, scan_id=None,
        decision_date=utc_date(rec.created_at),
        rec_id=rec.id,
        reject_reason=rationale.splitlines()[0][:160] if rationale else "",
        failed_gates=failed_gate_names(rec.gate_report))


# --- price-series primitives ----------------------------------------------

def _index_on_or_before(bars: list[Bar], day: date) -> Optional[int]:
    return next((i for i in range(len(bars) - 1, -1, -1)
                 if bars[i].day <= day), None)


def _returns_by_day(bars: list[Bar]) -> dict[date, float]:
    out: dict[date, float] = {}
    for prev, cur in zip(bars, bars[1:]):
        if prev.close > 0:
            out[cur.day] = cur.close / prev.close - 1.0
    return out


def market_beta(stock_bars: list[Bar], index_bars: list[Bar], asof: date,
                window_days: int = BETA_WINDOW_TRADING_DAYS,
                min_days: int = BETA_MIN_DAYS) -> Optional[float]:
    """Sensitivity of the stock to the index over the `window_days` trading
    days ending on or before `asof`. None when the overlap is too thin or the
    index didn't move — better than a number nobody can trust."""
    stock = _returns_by_day(stock_bars)
    index = _returns_by_day(index_bars)
    days = sorted(d for d in stock.keys() & index.keys() if d <= asof)
    days = days[-window_days:]
    if len(days) < min_days:
        return None
    sr = [stock[d] for d in days]
    ir = [index[d] for d in days]
    mean_s = sum(sr) / len(sr)
    mean_i = sum(ir) / len(ir)
    var_i = sum((x - mean_i) ** 2 for x in ir)
    if var_i <= 0:
        return None
    cov = sum((a - mean_s) * (b - mean_i) for a, b in zip(sr, ir))
    return cov / var_i


def atr_pct_at(bars: list[Bar], asof: date, period: int = 14) -> Optional[float]:
    """Average true range as a % of price, as of `asof`.

    Recomputed from the same history the returns come from rather than read
    off the stored gate report: enrichment-cap drops never had a gate report,
    and a single definition keeps z comparable across every cohort.
    """
    end = _index_on_or_before(bars, asof)
    if end is None or end < period:
        return None
    trs: list[float] = []
    for i in range(end - period + 1, end + 1):
        prev_close = bars[i - 1].close
        bar = bars[i]
        trs.append(max(bar.high - bar.low,
                       abs(bar.high - prev_close),
                       abs(bar.low - prev_close)))
    close = bars[end].close
    if close <= 0:
        return None
    return (sum(trs) / len(trs)) / close * 100.0


def z_score(alpha_pct: float, atr_pct: float,
            horizon_days: int) -> Optional[float]:
    """Alpha expressed in units of the stock's own typical move over the same
    span (§5.2(3) 第2段: `z = α ÷ (ATR% × √営業日数)`). Without this, a
    volatile small cap and a mega cap moving the same % would look like the
    same size of miss, and the volatile names would dominate every review
    queue purely by being volatile."""
    denom = atr_pct * math.sqrt(horizon_days)
    if denom <= 0:
        return None
    return alpha_pct / denom


# --- checkpoint collection -------------------------------------------------

def collect_checkpoints(
    tracked: Iterable[TrackedCandidate],
    provider,
    today: date,
    index_ticker: str,
    checkpoint: str,
    history_days: int = 400,
) -> tuple[list[CheckpointResult], int, dict[str, int]]:
    """Returns (results, pending, censored-per-cohort).

    `pending` = the checkpoint hasn't elapsed yet (expected, not a bug).
    `censored` = the price fetch failed or returned no usable bars
    (delisting, ticker change, acquisition, API outage). Kept separate and
    reported, never folded into `pending`: a ticker whose history vanished is
    disproportionately likely to be the worst performer in its cohort, so
    dropping it silently biases every cohort mean upward.
    """
    horizon_days = CHECKPOINT_TRADING_DAYS[checkpoint]
    try:
        index_bars = provider.daily_history(index_ticker, days=history_days)
    except Exception:
        index_bars = []

    results: list[CheckpointResult] = []
    pending = 0
    censored = {c: 0 for c in COHORTS}
    min_wait = min_calendar_days_for_trading_days(horizon_days)

    for t in tracked:
        if (today - t.decision_date).days < min_wait:
            pending += 1
            continue
        try:
            bars = provider.daily_history(t.ticker, days=history_days)
        except Exception:
            censored[t.cohort] = censored.get(t.cohort, 0) + 1
            continue
        raw = forward_return(bars, t.decision_date, horizon_days)
        if raw is None:
            censored[t.cohort] = censored.get(t.cohort, 0) + 1
            continue

        base_idx = _index_on_or_before(bars, t.decision_date)
        price_at_decision = bars[base_idx].close if base_idx is not None else None
        end_idx = base_idx + horizon_days if base_idx is not None else None
        price_at_checkpoint = (bars[end_idx].close
                               if end_idx is not None and end_idx < len(bars)
                               else None)
        checkpoint_date = (bars[end_idx].day
                           if end_idx is not None and end_idx < len(bars)
                           else None)

        beta = (market_beta(bars, index_bars, t.decision_date)
                if index_bars else None)
        bench = (forward_return(index_bars, t.decision_date, horizon_days)
                 if index_bars else None)
        atr = atr_pct_at(bars, t.decision_date)

        alpha = (raw - beta * bench
                 if beta is not None and bench is not None else None)
        z = (z_score(alpha, atr, horizon_days)
             if alpha is not None and atr is not None else None)

        results.append(CheckpointResult(
            ticker=t.ticker, cohort=t.cohort, scan_id=t.scan_id,
            decision_date=t.decision_date, checkpoint=checkpoint,
            checkpoint_date=checkpoint_date, horizon_days=horizon_days,
            price_at_decision=price_at_decision,
            price_at_checkpoint=price_at_checkpoint,
            raw_return_pct=raw, benchmark_return_pct=bench, beta=beta,
            beta_window=BETA_WINDOW_TRADING_DAYS, atr_pct=atr,
            alpha_pct=None if alpha is None else round(alpha, 4),
            z=None if z is None else round(z, 4),
            direction=None if z is None else ("up" if z >= 0 else "down"),
            screened_candidate_id=t.screened_candidate_id, rec_id=t.rec_id,
            reject_reason=t.reject_reason, failed_gates=t.failed_gates,
            score=t.score))
    return results, pending, censored


def to_drop_review(result: CheckpointResult,
                   reviewer_model: str = "") -> DropReview:
    """Freeze one measurement into a storable review (§5.2(3)).

    Only the numbers; the investigation fields stay empty until [3] fills
    them. A result with no alpha is refused: that is a censored fetch, not a
    verdict, and storing it would put an empty judgement into the counts [4]
    acts on.
    """
    if result.alpha_pct is None or result.z is None or result.direction is None:
        raise ValueError(
            f"{result.ticker} at {result.checkpoint} was never scored "
            "(alpha/z unavailable) — censored, not reviewable")
    return DropReview(
        screened_candidate_id=result.screened_candidate_id,
        scan_id=result.scan_id,
        rec_id=result.rec_id,
        ticker=result.ticker,
        cohort=result.cohort,
        checkpoint=result.checkpoint,
        checkpoint_date=result.checkpoint_date,
        decision_date=result.decision_date,
        horizon_days=result.horizon_days,
        price_at_decision=result.price_at_decision,
        price_at_checkpoint=result.price_at_checkpoint,
        raw_return_pct=result.raw_return_pct,
        benchmark_return_pct=result.benchmark_return_pct,
        beta=result.beta,
        beta_window=result.beta_window,
        atr_pct=result.atr_pct,
        alpha_pct=result.alpha_pct,
        z=result.z,
        direction=result.direction,
        reviewer_model=reviewer_model)


def with_peer_baseline(
        results: list[CheckpointResult]) -> list[CheckpointResult]:
    """Attach the third-layer basis (§5.2(3) 第3段): the equal-weight mean
    raw return of every candidate from the same scan and checkpoint. This is
    what separates "the earnings-surprise universe was hot this week" from
    "our choice within it was good". A candidate is its own population when
    it is the only survivor of its scan, so its peer excess is 0 — "better
    than itself" is not a finding."""
    groups: dict[tuple[Optional[int], str], list[float]] = {}
    for r in results:
        groups.setdefault((r.scan_id, r.checkpoint), []).append(
            r.raw_return_pct)
    means = {k: sum(v) / len(v) for k, v in groups.items()}
    return [
        replace(r,
                peer_baseline_pct=round(means[(r.scan_id, r.checkpoint)], 4),
                peer_excess_pct=round(
                    r.raw_return_pct - means[(r.scan_id, r.checkpoint)], 4))
        for r in results
    ]


# --- attribution -----------------------------------------------------------

def _summarize(alphas: list[float], zs: list[float],
               alpha_missing: int = 0) -> dict:
    if not alphas:
        return {"n": 0, "mean_alpha": None, "median_alpha": None,
                "up_outliers": 0, "down_outliers": 0,
                "n_alpha_missing": alpha_missing}
    ordered = sorted(alphas)
    n = len(ordered)
    mid = n // 2
    median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "n": n,
        "mean_alpha": round(sum(ordered) / n, 2),
        "median_alpha": round(median, 2),
        # Both directions are always reported together (§5.2(3)): a gate whose
        # rejects rose 3 times and fell 9 is a gate that is working.
        "up_outliers": sum(1 for z in zs if z >= Z_THRESHOLD),
        "down_outliers": sum(1 for z in zs if z <= -Z_THRESHOLD),
        "n_alpha_missing": alpha_missing,
    }


def attribute_by_cohort(
        results: list[CheckpointResult]) -> dict[str, dict]:
    """Which funnel stage's leftovers performed best. Every cohort is present
    even at n=0 so an empty group reads as "no data yet", not as absence of
    the group."""
    out: dict[str, dict] = {}
    for cohort in COHORTS:
        mine = [r for r in results if r.cohort == cohort]
        out[cohort] = _summarize(
            [r.alpha_pct for r in mine if r.alpha_pct is not None],
            [r.z for r in mine if r.z is not None],
            alpha_missing=sum(1 for r in mine if r.alpha_pct is None))
    return out


def attribute_by_gate(results: list[CheckpointResult]) -> dict[str, dict]:
    """Which specific gate dropped the outliers. A cohort mean says "the
    gates are costing us"; only this says *which* gate to look at. A
    candidate that failed several gates counts under each — whether it would
    have passed had we relaxed just one is not knowable from the record, so
    attributing it to only the first would invent precision."""
    alphas: dict[str, list[float]] = {}
    zs: dict[str, list[float]] = {}
    missing: dict[str, int] = {}
    for r in results:
        for gate in r.failed_gates:
            if r.alpha_pct is None:
                missing[gate] = missing.get(gate, 0) + 1
            else:
                alphas.setdefault(gate, []).append(r.alpha_pct)
                if r.z is not None:
                    zs.setdefault(gate, []).append(r.z)
    names = set(alphas) | set(missing)
    return {g: _summarize(alphas.get(g, []), zs.get(g, []), missing.get(g, 0))
            for g in sorted(names)}


def outliers(results: list[CheckpointResult],
             z_threshold: float = Z_THRESHOLD,
             direction: Optional[str] = None,
             cohorts: Optional[tuple[str, ...]] = INVESTIGATION_COHORTS
             ) -> list[CheckpointResult]:
    """Candidates whose move was large relative to their own volatility — the
    shortlist for the §5.2(3) [3] per-name investigation, ordered by |z|
    descending.

    `cohorts` defaults to INVESTIGATION_COHORTS, i.e. enrichment-cap drops
    are left out: reading those one at a time cannot tell you what to change,
    while the group mean can. Pass None for every cohort. `direction` filters
    to "up" or "down"; the default returns both, and callers are expected to
    keep reporting both.
    """
    hits = [r for r in results
            if r.z is not None and abs(r.z) >= z_threshold
            and (direction is None or r.direction == direction)
            and (cohorts is None or r.cohort in cohorts)]
    return sorted(hits, key=lambda r: abs(r.z), reverse=True)
