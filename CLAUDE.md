# Hawkeye — notes for Claude sessions

## What this is

Adversarial-verification investment decision system (catalyst-driven US
equities MVP). The core hypothesis and non-negotiables live in
`docs/INVESTMENT_DOCTRINE.md` and `docs/VERIFICATION_PROTOCOL.md` — read them
before changing behavior. The user-facing language is Japanese; system code,
docs, prompts, and commit messages are English.

## Communication style (mandatory for every response)

The user reads explanations as a human decision-maker, not as a code
reviewer. This applies to all explanations in this project — bug reports,
behavior descriptions, review summaries, everything.

1. **Don't lead with bare symbol names.** Avoid dumping function/variable/
   table names as the explanation itself (e.g. "`ensureGaConfigured` 内の
   `isDev` が `false` になる"). The reader can't tell what that means without
   already knowing the code.
2. **Explain the role/purpose first, in plain Japanese, then cite the
   symbol as a parenthetical.** State what the process is *for* and *when*
   it runs before naming it.
   - 悪い例: 「`ensureGaConfigured` 内の `isDev` が `false` になるため...」
   - 良い例: 「GA4の初期化を行う処理（`ensureGaConfigured` 関数）において、
     開発・プレビュー環境であることを識別するフラグ（`isDev` 変数）が
     `false`（本番環境判定）になってしまうため...」
3. **State root cause and user-visible impact**, not just code behavior —
   what changes on screen or in system behavior as a result.
4. **Explain domain/strategy terms the first time they come up in a
   session**, not just code symbols. This project has its own vocabulary
   (Bull / Adversary / Judge roles, gates, EV hurdle, thesis-accuracy,
   pre-registration, etc. — see `docs/INVESTMENT_DOCTRINE.md` and
   `docs/VERIFICATION_PROTOCOL.md`). When one of these appears for the
   first time in a conversation, give a one-line plain-language gloss of
   what that role/mechanism actually does before using it as shorthand
   (e.g. "Bull（強気側の主張だけを作る役割。Adversaryの反論は見えない）").

## Invariants (do not break)

1. **Pre-registration**: recommendation payloads in the ledger are immutable.
   Anything that happens later is a journal event. Never add code that
   UPDATEs a recommendation payload.
2. **The journal is hash-chained** — `Ledger.verify_chain()` must stay green.
3. **Code enforces what prompts request**: judge rules (`_judge_rule_check`)
   and risk vetoes (`build_position_plan`) mechanically overturn BUYs.
   If you strengthen a prompt rule, mirror it in code. `_judge_rule_check`
   matches addressed attacks by `Attack.id` (content-hashed in
   `parse_attack_report`, not the LLM's choice) — never re-introduce
   text/substring matching on `attack_statement`; a paraphrased response
   must still count as addressed (2026-07-28 fix, was silently overturning
   correct BUYs).
4. **Information separation**: Bull never sees attacks; Adversary sees only
   the written thesis; Judge sees only the record. Keep the three LLM calls
   stateless and separate.
5. **No autonomous trading.** The system recommends and records; the user
   executes. Don't add order placement.
6. **Missing data is `unverified`, never a silent pass** (gates). On a hard
   gate this fails closed — `GateReport.hard_failures` counts an unverified
   hard gate the same as a failed one, so the candidate never reaches the
   LLM tribunal on missing data alone (2026-07-28 fix).
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

- **2026-07-28** User asked for an architecture/design/code review to
  confirm the system can actually deliver on its stated goal. Ran 4
  independent parallel reviews (doc-vs-code consistency, architecture
  soundness, python code quality, adversarial investment-methodology
  audit). Two CRITICAL findings were reached independently by multiple
  reviewers with no shared context — treated as high-confidence, not
  coincidence:
  (1) `_judge_rule_check` enforced "severe attack addressed" via a
  60-character substring match against `attack_statement`, not semantic
  content. A judge that paraphrases (any real LLM) fails this check even
  after genuinely refuting the attack, silently overturning a correct BUY
  to PASS with an authoritative-looking "[RULE ENFORCEMENT]" note. This is
  the likely real cause of the 2026-07-14 "3/3 candidates PASSed via rule
  enforcement" result — previously read as a healthy outcome, now suspect.
  **Fixed**: `Attack` gets a stable, content-hashed `id`
  (`_content_id()`, sha256 of category+statement+evidence — deterministic,
  not random, so two independent parses of the same raw dict agree); the
  Judge must now cite `attack_id` in `addressed[]`; `_judge_rule_check`
  matches by id-set membership. Both API and session drivers render an
  ID-tagged, already-parsed attack view to the Judge instead of the
  Adversary's raw JSON. Regression tests added proving a paraphrased
  statement with the right id still counts, and a wrong/missing id still
  overturns BUY. Old ledger rows keep loading (`attack_id` defaults to
  `""`), unaffected since Recommendation payloads are immutable.
  (2) An unverified hard entry gate (e.g. missing market cap from a
  Finnhub free-tier gap) returned `passed=True, unverified=True` and was
  never counted as a hard failure — contradicting invariant 6 in spirit
  (labeled unverified, but not blocking). Investment risk: an illiquid or
  micro-cap candidate could reach the LLM tribunal and get a real BUY with
  no way to exit cleanly at the pre-registered stop. **Fixed (fail-closed,
  user's explicit choice over a soft unverified-count threshold)**:
  `GateReport.hard_failures` now includes unverified hard gates. Also
  exposed a latent bug in the shared test fixture `make_bars()` (stopped
  ~30 days short of `end` for n=300, masked until now by the old
  silent-pass gate behavior) — fixed to anchor exactly on `end`.
  Separately, the methodology audit found the *already-shipped*
  `hawkeye benchmark` cohort comparison — the actual Phase 0 kill-criterion
  measurement — had the same two failure modes `docs/MASTER_OVERVIEW.ja.md`
  §5.1 warns about for the *proposed* future feature: manual `evaluate`
  picks were never filtered out of viability stats despite
  `docs/ROADMAP.md` requiring it, and any ticker whose price history fetch
  failed (delisted/acquired/API outage) was silently dropped rather than
  flagged — survivorship bias, since failed-fetch tickers are
  disproportionately the worst performers. **Fixed**: new
  `hawkeye/scout/benchmark.py::collect_samples()`; `hawkeye benchmark`
  defaults to `--source scout` and reports per-cohort censored counts with
  an explicit survivorship-bias warning instead of a silent skip count.
  Full test suite: 79/79 green (one pre-existing, unrelated collection
  failure in `tests/test_llm_auth.py` — imports a function that doesn't
  exist in `hawkeye/tribunal/llm.py` — left unfixed, out of scope). The
  remaining review findings (doc-vs-code drift: a few factual inaccuracies
  in ARCHITECTURE.md/MASTER_OVERVIEW.ja.md; ~19 further architecture
  findings from portfolio-cap-not-enforced-across-concurrent-evaluations to
  ledger hash-chain race conditions to session-mode role separation being
  prose-enforced rather than code-enforced; code-quality HIGH findings
  around ledger concurrent-write races and silent case-file-read failures)
  were reported but not acted on this session — not yet triaged into this
  log or the backlog.
- **2026-07-14(c)** User asked for a "50%必達" strategy/tactics audit against
  `MASTER_OVERVIEW.ja.md`. Verdict: current design is a strong bias-removal
  experiment but the return math doesn't close — modeled ceiling ~+47%/yr
  under aggressive assumptions (p=0.55, 8 slots fully turning), realistic
  case ~+13.5%/yr once occupancy (~70%) and profit-taking-at-target (no
  trailing/runner logic, avg realized ~1.3R not 2R+) are modeled. Missing
  levers identified: trade-count (single detector = zero candidates in
  earnings off-season), right-tail capture (target hit = review, not
  partial-exit-and-trail), and a pre-registered path to raise risk_pct
  (currently ~1/43 of full Kelly at p=0.55/2:1 payout — literal numbers
  belong to the reader's own priors, not a forecast). Also flagged missing
  defenses: no portfolio-level drawdown circuit breaker, no regime filter,
  no sector-concentration cap. Wrote `docs/STRATEGY_BACKLOG.ja.md`: full
  review + 12-item backlog (BL-01..12) tiered by cost/measurement-impact
  and sequenced against current dev state (Phase 0 has 0 open positions,
  3 evaluated candidates — cheapest possible time to make small
  risk-officer logic changes before cohort samples accumulate). Explicitly
  preserved the existing Phase-0 kill-criterion discipline: detector
  diversification and primary-source deep-reading (the two biggest
  offense levers) stay gated behind the 50-evaluated-candidates checkpoint
  already in ROADMAP.md — this audit does not override that. Nothing in
  the backlog has been implemented; awaiting user go-ahead per tier.
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
