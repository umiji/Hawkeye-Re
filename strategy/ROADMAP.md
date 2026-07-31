# Roadmap

Ordering principle (revised 2026-07-13): **validate the full funnel and the
core hypothesis first; automate and harden only what has proven worth
automating.** The funnel — candidate discovery → screening → adversarial
verification → recommend-or-pass → performance monitoring → attribution —
must run end-to-end from day one, because the thing under test is the
funnel's judgment, not any single stage. Manual candidate entry alone would
contaminate the experiment with human selection bias at the top of the
funnel.

Early phase gates are **process metrics, not P&L** (small-n P&L is noise,
and promoting phases on lucky returns contradicts the project's own
skill-vs-luck doctrine). P&L enters the criteria only once sample size
justifies it.

## Phase 0 — Full-funnel viability validation (current)

Everything runs, manually triggered:

- `hawkeye scout` — mechanical candidate discovery (earnings-surprise screen
  → gates → ranked shortlist), funnel counts recorded per scan
- `hawkeye evaluate` / `scout --evaluate N` — adversarial tribunal
- `hawkeye check` — sentinel on open positions
- `hawkeye benchmark` — forward returns of BUY vs tribunal-PASS vs
  gate-reject cohorts: the primary viability metric. The BUYs must beat the
  candidates the system rejected, or the screening/judgment logic is not
  adding value regardless of book P&L
- `hawkeye calibration` — Brier + quadrant attribution

**Candidate sourcing rule:** scouted candidates are the experiment; manual
`evaluate` entries are allowed but are a separate cohort (distinguishable in
the ledger via catalyst source), never mixed into viability stats.

**Exit criteria (to Phase 1):**

| Criterion | Bar |
|---|---|
| Scouted candidates evaluated by the tribunal | ≥ 50 |
| Completed full cycles (evaluate → outcome) | ≥ 20 |
| Funnel discrimination | BUY rate of gate-passing candidates in 10–40% |
| Cohort spread at 30d horizon | BUY mean return > tribunal-PASS mean return |
| Calibration sample | ≥ 50 resolved claims, overall Brier < 0.25 |
| Improvement loop closed | ≥ 1 doctrine/prompt revision derived from ledger evidence (commit) |
| Ledger integrity | `verify` green throughout |

**Project-level kill criterion (pre-registered, like any thesis):** if after
50 evaluated scouted candidates the BUY cohort does not beat the PASS cohort
AND Brier ≥ 0.25, stop and redesign the core logic before spending anything
on automation. A system that cannot out-select its own reject pile has no
business being automated.

## Phase 1 — Logic iteration under scheduled operation

Entered only with a positive viability signal. Two tracks in parallel:

- **Logic A/B**: revise screen thresholds / prompts / judge rules as config+
  prompt diffs; every revision is dated in git so cohort stats can be split
  pre/post-change. One change at a time.
- **Ops automation**: daily cron for scout + sentinel with pushed reports;
  weekly digest (P&L, funnel rates, kill-shot ledger, calibration drift);
  monthly blind re-underwrite of open positions (ownership context stripped).

Exit: 4+ weeks unattended daily operation, 100% signal-response discipline
(acted on or overridden in writing within 1 trading day), cumulative ≥ 100
scouted evaluations.

## Phase 2 — Process hardening

Each item starts only when the ledger shows the weakness it fixes:

- Role independence (different models for Bull vs Adversary) — when verdict
  patterns suggest same-model correlation
- Adversary incentive loop closed (kill-shots resolved against reality, track
  record fed back into its prompt)
- Automated claim verification for machine-checkable claim types
- Backtest harness over historical catalysts (deterministic layers replayed;
  LLM layers evaluated on frozen dossiers)
- Additional catalyst detectors beyond earnings surprise — insider clusters
  (Finnhub insider-transactions, free), news triage (an LLM screen over the
  news feed; near-zero marginal cost in session mode), guidance language,
  index events. Note: the earnings-only scout going quiet between seasons is
  acceptable — even desirable — during Phase 0: a catalyst strategy that
  cannot find a catalyst should sit in cash, not manufacture trades. Detector
  breadth is a Phase 2 investment, justified only after the Phase 0
  viability verdict.

## Phase 3 — Scale judgment (statistics phase)

≥ 100 closed trades. Now — and only now — outcome metrics join the gates:

- Mean per-trade return positive with bootstrap CI excluding zero
- skill_win share is the majority of winners (a lucky_win-driven book fails)
- Overall Brier ≤ 0.20 with a monotone calibration table
- Max drawdown within modeled bounds; zero unlogged rule overrides

Note: the 50%/year target itself is **not** a phase gate. A calibrated
positive-expectancy process below 50% justifies continued investment; a
lucky 50% does not.

## Phase 4 — Long-horizon book

Second tribunal profile (multi-year fundamental theses: different gates,
base rates, holding rules) sharing the same ledger and attribution
machinery. Opened only after Phase 3's evidence bar is met.

## Standing microservice migration path

Packages already communicate only via `hawkeye.contracts`. Extraction order
when scale demands: marketdata → scout → tribunal → sentinel/scheduler →
ledger (last; it is the source of truth).
