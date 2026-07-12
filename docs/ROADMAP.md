# Roadmap

## Phase 0 — Manual tribunal MVP (this repo, now)

User picks one ticker + catalyst, runs `hawkeye evaluate`, receives a BUY
proposal or a reasoned PASS, decides Yes/No, records fills, runs the daily
`check`, resolves claims, and accumulates attribution data.

Exit criteria: ≥ 20 full cycles recorded; calibration table populated;
first doctrine revision derived from ledger evidence rather than intuition.

## Phase 1 — Scheduled operation

- Daily cron: sentinel sweep + auto-fetched prices, results pushed to the
  user (report file / notification) instead of being pulled.
- Weekly digest: P&L, gate hit-rates, adversary kill-shot ledger,
  calibration drift.
- Monthly blind re-underwrite: re-run the tribunal on each open position
  with ownership context stripped ("would we buy this today?"); divergence
  between fresh verdict and held position triggers a review report.

## Phase 2 — Automated candidate discovery (Scout service)

- Earnings-calendar and news-feed scanning (Finnhub free endpoints) to
  propose CandidateBriefs without user input.
- Gate pre-screening across the day's events; only survivors reach the
  tribunal (LLM spend stays bounded).
- Dedup/cooldown memory so the same story isn't retried daily.

## Phase 3 — Process hardening

- Role independence: different models (or ensembles) for Bull vs Adversary;
  measure whether verdicts change.
- Close the Adversary incentive loop: resolve kill-shots against reality and
  feed the Adversary's own track record into its prompt.
- Automated claim verification for machine-checkable claim types
  (estimate revisions, guidance language, price-relative claims).
- Backtest harness replaying historical catalysts through the gates +
  deterministic layers (LLM layers evaluated on frozen historical dossiers).

## Phase 4 — Long-horizon book

Only after the short-cycle machine demonstrates calibration: a second
tribunal profile (multi-year fundamental theses, different gates, different
base rates), sharing the same ledger and attribution machinery.

## Standing microservice migration path

Each package already communicates only via `hawkeye.contracts`. Extraction
order when scale demands it: marketdata → tribunal → sentinel/scheduler →
ledger (last, since it is the source of truth).
