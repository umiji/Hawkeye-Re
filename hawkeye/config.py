"""Central configuration with doctrine defaults.

Every numeric rule in the investment doctrine
(strategy/INVESTMENT_DOCTRINE.md) lives here so it is pre-registered,
versioned, and testable — never buried in prompts or ad-hoc code.

Filesystem locations are NOT configuration in this sense; they live in
`hawkeye/paths.py`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class HawkeyeConfig:
    # --- Entry gates (deterministic, run before any LLM spend) ---
    min_price: float = 5.0                  # USD; avoid microcap noise
    min_market_cap: float = 300e6           # USD
    min_avg_dollar_volume: float = 10e6     # USD, 20-day average
    max_event_age_days: int = 10            # trading days since catalyst
    max_gap_pct: float = 25.0               # |move on event day|; larger = crowded (warn)
    max_atr_pct: float = 8.0                # 14d ATR / price (warn)
    earnings_warning_days: int = 7          # next earnings this close = warn

    # --- Risk officer (deterministic, holds veto power) ---
    default_risk_pct: float = 0.75          # % of NAV at risk per position (to stop)
    max_position_pct: float = 10.0          # % of NAV in a single name
    max_positions: int = 8
    min_reward_risk: float = 2.0            # (target-entry)/(entry-stop)
    min_expected_value_pct: float = 5.0     # scenario-weighted expected return
    max_holding_days: int = 45              # time stop for catalyst trades

    # --- Scout (candidate discovery screen) ---
    scout_days_back: int = 7                # scan window for earnings events
    scout_min_eps_surprise_pct: float = 5.0
    scout_min_revenue_surprise_pct: float = 0.0
    scout_max_enrich: int = 15              # candidates enriched with price data
                                            # (bounds free-tier API usage)

    # --- News fetch window (docs/MASTER_OVERVIEW.ja.md §5.2(5)) ---
    # Not doctrine — data-collection parameters. The window is anchored on
    # the catalyst date, not on "today": with a fixed today-minus-N window,
    # a candidate whose earnings landed near max_event_age_days could have
    # its earnings coverage crowded out by newer unrelated headlines.
    news_lead_days: int = 3                 # days before the catalyst to start
    news_max_items: int = 25                # items kept (nearest the catalyst)

    # --- Attribution ---
    thesis_accuracy_threshold: float = 0.6  # >= this fraction of claims true = "thesis right"

    # --- Phase 0 kill-criterion measurement ---
    # The ONE official horizon (trading days) for the BUY-vs-PASS-vs-REJECT
    # cohort comparison `hawkeye benchmark` uses to decide Phase 0 viability
    # (strategy/ROADMAP.md). Pinned so the measurement can't be quietly re-run at
    # a different horizon until the spread looks favorable (2026-07-29,
    # methodology-auditor finding H5). `--horizon` on the CLI still accepts
    # an override for exploration, but its output is labeled non-authoritative.
    phase0_benchmark_horizon_days: int = 30

    # --- Drop-candidate review (docs/MASTER_OVERVIEW.ja.md §5.2(3)) ---
    # Measurement parameters, not doctrine numbers: invariant 7 governs the
    # investment rules, and these describe how the screen is *scored*. The
    # checkpoints (T+5/T+10 trading days), the 250-day beta window and the
    # |z| >= 1.5 bar live in `hawkeye/scout/drop_review.py` rather than here,
    # precisely because they must not be tunable per run.
    drop_review_index_ticker: str = "SPY"
    # Nothing gets re-tuned off a handful of names (§5.2(3) 過剰最適化の歯止め).
    # Counted per `miss_category`, not per funnel stage: the unit that has to
    # reach 20 is "the same cause, seen 20 times", because that is what names
    # the knob to turn. A stage tally mixes causes and would authorize a
    # revision nobody can point at (renamed 2026-08-01; value unchanged).
    drop_review_min_samples_per_category: int = 20

    # --- LLM ---
    model: str = "claude-opus-4-8"

    @staticmethod
    def from_env() -> "HawkeyeConfig":
        model = os.environ.get("HAWKEYE_MODEL", "claude-opus-4-8")
        return HawkeyeConfig(model=model)
