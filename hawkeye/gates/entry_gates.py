"""Deterministic entry gates.

These run BEFORE any LLM is invoked. A hard failure kills the candidate for
free — no narrative, however good, can argue its way past a liquidity or
freshness gate. Missing data never silently passes: it is flagged
``unverified`` so the judge and the user both see the hole.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import Catalyst, GateReport, GateResult, MarketSnapshot


def _minimum(name: str, value: Optional[float], threshold: float, hard: bool) -> GateResult:
    if value is None:
        return GateResult(name=name, passed=True, hard=hard, unverified=True,
                          threshold=threshold, note="data unavailable — verify manually")
    return GateResult(name=name, passed=value >= threshold, hard=hard,
                      value=value, threshold=threshold)


def _maximum(name: str, value: Optional[float], threshold: float, hard: bool) -> GateResult:
    if value is None:
        return GateResult(name=name, passed=True, hard=hard, unverified=True,
                          threshold=threshold, note="data unavailable — verify manually")
    return GateResult(name=name, passed=value <= threshold, hard=hard,
                      value=value, threshold=threshold)


def run_entry_gates(snapshot: MarketSnapshot, catalyst: Catalyst,
                    config: HawkeyeConfig, today: Optional[date] = None) -> GateReport:
    today = today or date.today()
    results: list[GateResult] = []

    # Hard gates — structural: we do not trade what we cannot exit,
    # what is too small to be efficient, or what is no longer fresh.
    results.append(_minimum("min_price", snapshot.price, config.min_price, hard=True))
    results.append(_minimum("min_market_cap", snapshot.market_cap,
                            config.min_market_cap, hard=True))
    results.append(_minimum("min_avg_dollar_volume", snapshot.avg_dollar_volume_20d,
                            config.min_avg_dollar_volume, hard=True))
    results.append(_maximum("catalyst_freshness_days",
                            float(snapshot.days_since_event) if snapshot.days_since_event is not None else None,
                            float(config.max_event_age_days), hard=True))

    # Soft gates — the judge must weigh these, but they don't auto-kill.
    gap = abs(snapshot.gap_on_event_pct) if snapshot.gap_on_event_pct is not None else None
    r = _maximum("event_gap_not_extreme", gap, config.max_gap_pct, hard=False)
    if not r.passed:
        r.note = "very large event-day move — repricing may be complete and positioning crowded"
    results.append(r)

    r = _maximum("volatility_sane", snapshot.atr_pct_14d, config.max_atr_pct, hard=False)
    if not r.passed:
        r.note = "elevated volatility — stop distance and sizing must reflect it"
    results.append(r)

    if snapshot.next_earnings_date is not None:
        days_to_earnings = (snapshot.next_earnings_date - today).days
        if 0 <= days_to_earnings <= config.earnings_warning_days:
            results.append(GateResult(
                name="earnings_proximity", passed=False, hard=False,
                value=float(days_to_earnings), threshold=float(config.earnings_warning_days),
                note="next earnings inside the entry window — binary event risk"))
        else:
            results.append(GateResult(name="earnings_proximity", passed=True, hard=False,
                                      value=float(days_to_earnings)))
    else:
        results.append(GateResult(name="earnings_proximity", passed=True, hard=False,
                                  unverified=True, note="next earnings date unknown"))

    return GateReport(results=results)
