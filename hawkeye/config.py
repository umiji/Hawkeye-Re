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
    # How many gate-passing candidates one scan tries to assemble. The
    # shortlist is ranked among these, and only the top few are ever argued,
    # so this is the size of the pool the ranking gets to choose from —
    # enrichment is what reveals the event-day reaction, worth up to 25 of
    # the score's points, and a pool of 3 would make that term decorative.
    scout_target_gate_passed: int = 15
    # Attempt ceiling, so a day where almost everything fails the gates
    # cannot walk the whole calendar. 3x the target: past that the screen is
    # into progressively weaker surprises and the run should say it stopped
    # short rather than grind through the free tier. Reaching this before
    # the target is reported, never silent.
    scout_max_enrich: int = 45
    # A surprise percentage is only as good as its denominator. Below this
    # absolute consensus the ratio measures the estimate, not the beat — a
    # REIT reporting FFO carries a GAAP EPS consensus near zero, which is why
    # a 2026-08-01 run was topped by +6958%, +5194% and +3459% readings.
    scout_min_abs_eps_estimate: float = 0.10
    # Actual and estimate can be on different accounting bases (gross vs net
    # revenue for lenders), which reads as a several-hundred-percent beat.
    # Past this, the number is treated as unverified rather than as a beat.
    scout_max_trusted_revenue_surprise_pct: float = 50.0

    # --- Earnings quality: the three legs (docs/design/MASTER_OVERVIEW.ja.md §5.3) ---
    # When the two vendors' EPS actuals are far enough apart to be worth
    # telling the reader about. Both conditions must hold: on a $0.30 EPS, one
    # cent of rounding is 3%. Taken from the measured distribution of a
    # 50-name sample on 2026-08-02 — actual disagreement runs 0.9%/1.1%
    # (rounding) and then jumps to 2.9%.
    #
    # This is a REPORTING threshold, not a judgment one. It stopped deciding
    # anything on 2026-08-07 with the move to one vendor per print: a reading
    # now stands on one vendor's actual over that same vendor's consensus, so
    # the other vendor's figure cannot make it better or worse. It is still
    # measured and still named, because the gap is usually GAAP against an
    # adjusted basis and that is a fact the Adversary should be able to attack.
    #
    # Three switches went with the two-vendor rule and are not replaced:
    # `earnings_consensus_dispute_pct` (two consensus figures to compare),
    # `earnings_actual_dispute_blocks` and
    # `earnings_single_source_consensus_blocks` (both already off, and with one
    # vendor per print neither had anything left to select).
    earnings_actual_dispute_pct: float = 2.0
    earnings_actual_dispute_abs_usd: float = 0.01
    # A consensus this thin is one analyst's opinion wearing the word
    # "consensus" — INVH's was built from exactly one, and only a
    # pre-registered row with an analyst count reveals it.
    earnings_min_analysts: int = 3
    # How many names one scan asks the earnings feed about, best score first.
    # One request per print, and a scan window holds ~350 a day, so this is a
    # run-duration and rate-limit ceiling rather than a doctrine number. Names
    # past it are reported, never dropped silently.
    #
    # 50 rather than 200 (2026-08-07): the feed decides the RANKING now, not
    # just a confirmation, so this is the size of the pool the top 15 are drawn
    # from. Measured at 184 req/min with no throttling, 50 costs ~16s.
    scout_max_whispers: int = 50
    # How long a print whose numbers have not arrived is held open before it
    # is given up on, measured from the ANNOUNCEMENT (hawkeye/scout/
    # waiting.py). The feed publishes a company's new quarter roughly a day
    # late — 16 of 16 names that reported on the morning of 2026-08-05 still
    # answered with their May quarter — so a print read once and dropped would
    # discard exactly what the funnel is looking for. Two days covers that lag
    # twice over while still ending the wait for data that is never coming.
    earnings_actual_wait_hours: int = 48
    # How long after a print its figures are still watched for a CORRECTION
    # (task 8.5). A different rule from the wait above, which bounds how long
    # a print with no figures at all is held: this one bounds how long a print
    # that already reported can have its numbers restated under us. ADEA's
    # 2026-Q2 EPS moved $0.34 -> $0.42 the day after it announced, so the
    # window has to cover at least the following session; beyond a couple of
    # days a restatement is a different question, and reopening a quarter the
    # ledger has finished with on a scan nobody ran for that purpose is the
    # failure this bound exists to prevent.
    actual_revision_watch_hours: int = 48
    # Guidance is a small bonus and never a penalty: absence is the normal
    # case, has no structured source, and must not become a hidden gate
    # (§5.3 決定3). Deliberately far below the EPS/revenue contributions.
    guidance_beat_score: float = 5.0
    # The feed's unofficial expectation ("whisper"), which sits ABOVE consensus
    # on every name measured so far (11 of 11), so clearing it is the stricter
    # test. Paid as a bonus on top of the consensus-based surprise and never as
    # a replacement for it: swapping the denominator would lower every ratio,
    # and a bonus on a silently reduced base cannot be told apart from one that
    # is merely refilling what the swap removed.
    #
    # `weight` is points per 1% above the whisper; `cap` is the most the bonus
    # can ever add — a fifth of the EPS cap (50) and half the revenue cap (20),
    # so it reorders near-ties and never buys a slot from a genuinely larger
    # surprise. Deliberately small because the evidence is contested:
    # Bagnoli/Beneish/Watts (1999) predates Reg FD, later work reports the
    # effect reversing, and the paper measured the PRE-print gap while this
    # measures the print clearing it.
    #
    # 0.25 was measured, not chosen for looks. On the 47-name post-print corpus
    # (2026-08-05) 11 records carry a whisper and 7 cleared it, by 3.0 / 5.1 /
    # 11.1 / 14.2 / 25.2 / 26.8 / 103.4 percent. At a weight of 1.0 the cap
    # bound on 5 of those 7, which would have made the bonus a flat +10 in
    # nearly every case — the step function task 8 explicitly rejected, because
    # a step ignores magnitude once it is crossed. At 0.25 the cap binds only
    # on ALB's +103.4%, where the percentage has stopped being informative.
    whisper_beat_weight: float = 0.25
    whisper_beat_cap: float = 10.0

    # --- Consensus pre-registration (docs/design/MASTER_OVERVIEW.ja.md §6.1(D)) ---
    # Runs are manual, so a strict T-1 window loses a print's snapshot
    # permanently on any missed day — and a snapshot missed before the
    # release can never be taken afterwards.
    consensus_capture_business_days: int = 2
    # Skip pre-registering names the entry gates have already refused for a
    # STRUCTURAL reason — below the price/market-cap/liquidity floors, i.e.
    # companies no print could turn into a position (§6.1(E)). A live
    # two-business-day window holds ~855 names and each costs a Yahoo call.
    prereg_skip_non_targets: bool = True
    # After this many days without a capture run, the window also covers
    # TODAY's prints. It normally starts tomorrow, because today's US prints
    # land in the evening JST and yesterday's run already registered them —
    # but a gap means nobody did, and the window is built from the local
    # (JST) date while the calendar's days are US market days, so today's
    # prints would otherwise fall permanently between the two runs.
    consensus_capture_include_today_after_days: int = 2
    # How long that verdict stands before the name is looked at again. The
    # asymmetry decides it: a wrong exclusion loses a consensus history that
    # can NEVER be rebuilt, a wrong inclusion costs one API call. Roughly two
    # reporting quarters, so a company that grew past the floors is picked up
    # within a quarter of doing so.
    stock_triage_ttl_days: int = 90

    # --- News fetch window (docs/design/MASTER_OVERVIEW.ja.md §5.2(5)) ---
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

    # --- Drop-candidate review (docs/design/MASTER_OVERVIEW.ja.md §5.2(3)) ---
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
