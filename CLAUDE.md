# Hawkeye — notes for Claude sessions

## What this is

Adversarial-verification investment decision system (catalyst-driven US
equities MVP). The core hypothesis and non-negotiables live in
`docs/INVESTMENT_DOCTRINE.md` and `docs/VERIFICATION_PROTOCOL.md` — read them
before changing behavior. The user-facing language is Japanese; system code,
docs, prompts, and commit messages are English.

## Invariants (do not break)

1. **Pre-registration**: recommendation payloads in the ledger are immutable.
   Anything that happens later is a journal event. Never add code that
   UPDATEs a recommendation payload.
2. **The journal is hash-chained** — `Ledger.verify_chain()` must stay green.
3. **Code enforces what prompts request**: judge rules (`_judge_rule_check`)
   and risk vetoes (`build_position_plan`) mechanically overturn BUYs.
   If you strengthen a prompt rule, mirror it in code.
4. **Information separation**: Bull never sees attacks; Adversary sees only
   the written thesis; Judge sees only the record. Keep the three LLM calls
   stateless and separate.
5. **No autonomous trading.** The system recommends and records; the user
   executes. Don't add order placement.
6. **Missing data is `unverified`, never a silent pass** (gates).
7. Doctrine numbers live in `hawkeye/config.py` only. A rule change is a
   config diff with rationale in the commit message.

## Layout

`contracts` (shared models — the only inter-package interface) · `marketdata`
(Yahoo/Finnhub + indicators) · `gates` · `tribunal` (LLM roles + pipeline) ·
`risk` · `ledger` (SQLite store + scoring) · `sentinel` · `reports` (Japanese
rendering) · `cli`.

## Dev

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest          # fully offline; ScriptedLLM + StaticProvider
```

LLM client: `claude-opus-4-8`, adaptive thinking, structured outputs.
Pipeline parsers clamp/normalize all LLM output — keep new LLM fields going
through a parser, never straight into a contract model.

## Session mode (/hawkeye-run)

The tribunal can be driven by a Claude Code session instead of the API:
`.claude/skills/hawkeye-run/SKILL.md` orchestrates `hawkeye case
open/step/submit`, spawning one fresh subagent per role. Invariants 3/4
apply doubly here: `casefile.write_package()` is the only place deciding
what a role sees, and `case submit` runs the same parsers/rule checks as
API mode. Never have the orchestrating session author or edit role JSON.

## Governance (added 2026-07-14)

Before any new feature or design change (not small bugfixes/typos), update
`docs/MASTER_OVERVIEW.ja.md` §4 (As-Is) and §5 (gap table) — or draft a
short design note — and get user approval BEFORE implementing. This
document was requested after the user flagged that prior sessions
implemented features unilaterally without ever presenting the full
picture (To-Be architecture, As-Is gap, and *why* the design should work)
in one place. Keep §4/§5 current as capabilities land.

## Session hand-off log

Record decisions and insights at the end of each working session
(newest first).

- **2026-07-14(b)** User flagged that development had proceeded feature-by-
  feature with no single presentation of the full picture (To-Be
  architecture, As-Is gap, investment principle, data model, user
  workflows) — described feeling unable to oversee direction. Wrote
  `docs/MASTER_OVERVIEW.ja.md`: requirements recap, honest explanation of
  *why* the design should improve returns (bias removal via role
  separation, asymmetric R:R enforcement, EV hurdle separating "good
  story" from "good trade", mechanical candidate sourcing, calibration/
  skill-vs-luck as the real compounding edge — explicitly framed as "a
  system that promises a guaranteed return is a red flag, not a feature"),
  full To-Be vs As-Is gap table, ER diagram, two sequence diagrams, and
  per-task user workflows (candidate selection / holding / post-sale /
  system improvement / retrospective). Published as an artifact too.
  Added the governance rule above per the user's explicit request.
- **2026-07-14** First real (non-synthetic) session-mode run: 3/3 scouted
  candidates PASSed, all via Judge rule enforcement (unaddressed severity-5
  attacks / sucker-test failures) — a healthy outcome, but user correctly
  flagged that Bull/Adversary were arguing over news headlines+summaries
  only, with no structured fundamentals. Risk: can't distinguish "process
  correctly rejecting weak setups" from "process structurally incapable of
  ever producing BUY because inputs are too thin" — exactly the H1/H2
  ambiguity the Phase 0 kill criterion exists to catch. Response:
  (1) wired scout's computed eps/revenue surprise into MarketSnapshot as
  structured fields (previously only in catalyst.description prose);
  (2) added InsiderActivity (net open-market buy/sell, Finnhub
  insider-transactions) and AnalystTrend (recommendation counts,
  Finnhub recommendation) to CandidateBrief, wired via duck-typed
  provider.insider_activity()/analyst_trend() (optional — Yahoo-only
  providers degrade to None, never raise); both may require a paid
  Finnhub tier, undocumented which — code treats absence as unverified,
  not "no activity". Prompts updated to cite these fields and to trust
  structured surprise numbers over prose-implied ones. (3) Added
  `hawkeye review-passes` — individual postmortem flagging PASSed/declined
  tickers that moved >= threshold afterward, distinct from `benchmark`'s
  aggregate cohort stats; a big rally on a PASSed name is a signal the
  PASS call may have been wrong (or new info emerged after — check
  `hawkeye show` before concluding either way). 14 new tests (72 total).
- **2026-07-13(b)** Session mode: user runs Hawkeye inside Claude Code on
  subscription (no metered API key). Added `casefile` (case open/step/submit
  CLI) + `/hawkeye-run` skill; API and session drivers share
  `assemble_recommendation()` so records are identical and comparable
  (`model` field distinguishes engines). Scout's earnings-only coverage gap
  acknowledged: off-season quiet is BY DESIGN for Phase 0 (no catalyst = no
  trade); additional detectors (insider clusters, news triage — cheap in
  session mode) queued behind the Phase 0 viability verdict.
- **2026-07-13** Reprioritized on user direction: the full funnel (scout →
  tribunal → monitor → attribution) must be validated BEFORE any automation.
  Added `scout` (Finnhub earnings-calendar surprise screen → gates → ranked
  shortlist, funnel counts persisted in `scans`) and `benchmark` (forward
  returns of BUY vs tribunal-PASS vs gate-reject cohorts — the Phase 0
  viability metric; BUYs must beat the reject pile). ROADMAP.md rewritten
  with measurable phase gates + a project-level kill criterion. Manual
  `evaluate` candidates stay a separate cohort (catalyst.source) so human
  picks never contaminate system-validation stats.
- **2026-07-12** Initial build: contracts, gates, tribunal (Bull/Adversary/
  Judge with rule enforcement), risk officer, hash-chained ledger,
  sentinel, JA reports, CLI, 44 offline tests. Doctrine v1: risk 0.75%/pos,
  RR≥2, EV≥5%, time stop 45d, thesis-accuracy threshold 0.6.
