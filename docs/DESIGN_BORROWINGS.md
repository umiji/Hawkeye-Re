# Design borrowings: what to take from the surveyed projects

Third document in the series. `LANDSCAPE.md` catalogued what exists;
`FINDINGS.md` verified where Hawkeye is broken. **This one is the design work**:
concrete architecture, strategy, and agent-design patterns lifted from the
surveyed repositories, with what Hawkeye does today and what to change.

Method: read the actual repository structure and source — directory trees,
config files, routing logic, risk managers, orchestrator manifests — not
README marketing. Where a borrowing happens to fix a defect already recorded
in `FINDINGS.md`, that is noted, but the organizing principle here is *their
design*, not our bugs.

Each item states: **source → pattern → Hawkeye today → proposal → why it fits
the invariants.** Nothing here asks to break an invariant; several items exist
because an invariant is currently asserted but not mechanically held.

---

# Part 1 — Architecture

## A-1. Provider standardization: `QueryParams` + `Data` per data type
**Source: OpenBB (71.3k★) — `openbb_core/provider/standard_models/`**

OpenBB defines ~150 standard models, one per data type
(`equity_historical`, `company_filings`, `analyst_search`, …), each as a
**two-class pair**: a `QueryParams` class fixing the standardized inputs and a
`Data` class fixing the standardized output fields. Every provider adapter
translates its native response into that contract. Swapping or adding a
provider is transparent to call sites.

**Hawkeye today** (`marketdata/base.py:27-38`): one flat `Protocol` with three
methods — `daily_history`, `profile`, `news`. `profile()` returns a bare
`dict` documented as "best-effort: {name, sector, market_cap,
next_earnings_date}". There is no schema, no per-field provenance, and no way
to add a fourth data type without widening the Protocol and every implementer.

This is the actual blocker for F-10: adding estimate revisions, short volume,
options, and insider filings would mean either a five-method-wider Protocol or
call sites bypassing the abstraction entirely.

**Proposal.** Adopt the pair pattern at Hawkeye's scale — six types, not 150:
`PriceHistory`, `CompanyProfile`, `NewsFeed`, `AnalystEstimates`,
`ShortInterest`, `OptionsSummary`. Each gets a pydantic `Data` model in
`contracts/` (the codebase already has pydantic everywhere) and a provider
method returning it. `profile()`'s untyped dict becomes a typed
`CompanyProfile` — which also means missing fields are `Optional` at the type
level rather than a `.get()` returning `None` by accident, tightening
invariant 6 at the source rather than at the gate.

**Fits:** `contracts` is already declared "the only inter-package interface"
(CLAUDE.md). This extends that rule down into marketdata, where it currently
stops at a dict.

## A-2. Vendor routing per data category
**Source: TradingAgents (95.3k★) — `default_config.py`**

Their config binds a vendor **per data category**, not per system:
core stock → `yfinance`, technical indicators → `yfinance`, fundamentals →
`yfinance`, news → `yfinance`, macro → `fred`, prediction markets →
`polymarket`. Changing where fundamentals come from is a config edit.

**Hawkeye today**: the composite policy is prose in `DATA_SOURCES.md:15-16`
("Yahoo for prices always; Finnhub for profile/news/earnings when a key is
present, Yahoo news otherwise") and hardcoded in the provider construction.

**Proposal.** A `data_vendors` mapping in `config.py` alongside the doctrine
numbers:

```
prices: yahoo · profile: finnhub · news: finnhub|yahoo
estimates: global_stock_data · short_interest: finra
options: cboe · filings: sec_edgar · macro: treasury
```

Two payoffs beyond tidiness. First, the source of every dossier field becomes
**pre-registered and versioned like a doctrine number**, so "the estimates
vendor changed" is a git diff rather than an unexplained regime break in the
ledger. Second, it is the natural place to record the compliance tier that
`global-stock-data` publishes per source (S / B / C — see F-10), so tier C
"personal research only" is a declared property of the config, not tribal
knowledge.

## A-3. Fallback chain + opt-in local cache
**Source: Vibe-Trading (29.2k★) — 18+ providers as a fallback chain with
opt-in local data cache**

**Hawkeye today**: a single composite with a manual override escape hatch
(`--price`, `--market-cap`, … per `DATA_SOURCES.md:23-25`). When a free
endpoint is down, the human patches it from the CLI. Scout treats enrichment
failure as rejection (`scout/scout.py:114-117`) — a data outage silently
removes candidates from the funnel, which quietly biases the very funnel
statistics Phase 0 is measuring.

**Proposal.** Declare an ordered chain per category (A-2 makes this natural)
and record **which vendor actually answered** in the snapshot. Add a local
cache keyed by (ticker, date) — Hawkeye's dossiers are point-in-time by
nature, so caching is not just speed: it makes a scan **replayable**, which
matters for the backtest harness on Phase 2's list.

Also: separate "candidate rejected by doctrine" from "candidate lost to an
outage" in `ScoutResult`. Today both land in `rejected` with a reason string;
only the first belongs in funnel-discrimination statistics.

## A-4. Two-level model routing as config
**Source: TradingAgents — `deep_think_llm` / `quick_think_llm`, plus
`openai_reasoning_effort` / `anthropic_effort` / `google_thinking_level`.
Corroborated by orallexa: Haiku for Bull/Bear, Sonnet at high effort for the
Judge, ~$0.003 per analysis.**

Both projects treat *which model and how hard it thinks* as a first-class,
per-role configuration key.

**Hawkeye today** (`tribunal/llm.py:36,44`): one `self.model` for all three
roles and `thinking={"type": "adaptive"}` hardcoded inside `complete_json`.
There is no seam to vary model or effort per role.

**Proposal.** Move model and effort into `HawkeyeConfig` as a per-role map,
and pass them through `complete_json`. This is a small refactor with three
separate payoffs:

1. It is the prerequisite for cross-model roles (G-3), which the deliberation
   research says is where most of the debate benefit actually lives.
2. It makes the thinking-budget experiment in F-08 *runnable* — currently
   there is no way to vary it.
3. It puts model identity in the same pre-registered, version-controlled place
   as the doctrine numbers, which matters because `Recommendation.model`
   already exists to make records comparable across engines.

## A-5. Run manifest + directory checkpointing + dry-run
**Source: `edge-pipeline-orchestrator` (claude-trading-skills)**

A seven-stage pipeline where each stage writes typed artifacts to its own
subdirectory, a `pipeline_run_manifest.json` captures the **full execution
trace**, review iterations accumulate in timestamped `reviews_iter_N/`
folders, and the whole thing supports **resumption from any stage** and a
**dry-run** that previews without committing exports.

**Hawkeye today** (`tribunal/casefile.py`): genuinely close already — cases
are directories, each role's system/input/schema/output are files, and
`next_role()` makes runs resumable. This is a good design and the survey
found nothing better.

**Proposal — three small additions.**
1. **Run manifest per case**: timestamps, which vendor answered each field,
   model+effort per role, prompt hashes. Hawkeye's ledger records *what was
   decided*; the manifest records *how it was produced*. Both are needed to
   diagnose a regime break in the calibration table months later, and prompt
   hashes are what let Phase 1's "split cohort stats pre/post prompt change"
   actually work.
2. **Dry-run**: render every role package and the deterministic tail without
   writing to the ledger. Today, testing a prompt change means either
   polluting the ledger or reading code.
3. **Retention of superseded role outputs.** `submit()` overwrites
   `case.thesis_raw` (`casefile.py:169-177`); a re-run of the Bull loses the
   prior draft. Keep them numbered. Invariant 1 protects the *recommendation*;
   the drafts behind it currently have no such protection.

## A-6. Expose Hawkeye over MCP, read-only by default
**Source: maverick-mcp (641★) — FastMCP server, 5 tool domains, "read-only by
default", tiered cache (memory → Redis → SQLite), trade journal as tools**

Their most transferable decision is not the tooling but the safety posture:
**nearly every tool is marked read-only**; only portfolio/watchlist/journal/
cache-clear mutate, and those require explicit confirmation.

**Hawkeye today**: session mode shells out to the `hawkeye` CLI. That works,
but the orchestrating session has whatever the shell has — including
`hawkeye decide`, `record-entry`, `close`, and `resolve-claim`, all of which
mutate the ledger.

**Proposal.** Expose the read side (`show`, `list`, `positions`, `check`,
`calibration`, `benchmark`, `verify`) as an MCP server, and **keep every
mutating command CLI-only and human-invoked**. This draws a mechanical line
where today there is only convention, and it directly serves invariant 5 (no
autonomous trading) plus the `/hawkeye-run` rule that the orchestrating session
must never author role JSON. An agent that *cannot* reach the write path is a
stronger guarantee than one instructed not to use it.

## A-7. Workflow manifests as artifacts
**Source: claude-trading-skills — `skills-index.yaml` (authoritative metadata
index) and `workflows/` holding five named multi-skill sequences:
`market-regime-daily`, `core-portfolio-weekly`, `swing-opportunity-daily`,
`trade-memory-loop`, `monthly-performance-review`**

Cadence is **data**, not prose: each workflow names its stage order and the
skills it composes.

**Hawkeye today**: the review cadence is prose in
`INVESTMENT_DOCTRINE.md:78-86` (daily `check`, weekly process health, monthly
re-underwrite, per-closed-trade attribution), and `/hawkeye-run` covers one
cycle. The weekly and monthly rituals — the ones that actually close the
improvement loop — have no artifact at all.

**Proposal.** A `workflows/` directory with manifests for `daily-check`,
`weekly-process-health`, `monthly-reunderwrite`, `per-trade-attribution`. Each
names its steps and its outputs. This turns "did we do the monthly
re-underwrite?" into a checkable fact — which is exactly the discipline
Hawkeye applies to positions and currently does not apply to itself.

---

# Part 2 — Investment strategy

## S-1. Volatility-scaled position limits with a correlation multiplier
**Source: ai-hedge-fund (62.6k★) — `src/agents/risk_manager.py`**

The single best design artifact found in the survey, and notable because it is
fully deterministic in a project that is otherwise all LLM personas:

- 60-day lookback → annualized volatility → **base position limit**:
  <15% vol → up to 25% of portfolio · 15–30% → 12.5–20% sliding ·
  30–50% → 5–15% · >50% → 10% max
- a **correlation matrix** across held tickers; each candidate's average
  correlation with active holdings sets a multiplier:
  ≥0.8 → **0.7×** · 0.6–0.8 → 0.85× · 0.4–0.6 → 1.0× · low → 1.05–1.10×
- `limit = portfolio_value × (vol_adjusted_pct × corr_multiplier)`,
  then `min(remaining_limit, available_cash)`
- output carries the **full intermediate calculation**, not just the answer

**Hawkeye today** (`risk/sizing.py:62-63`): the only portfolio-aware check is
`open_position_count >= max_positions`. Verified absent from the whole
codebase: aggregate risk, correlation, sector concentration. `sector` is
carried on every `CandidateBrief` and never read outside the model definition.

**Proposal.** Take this design more or less directly. Two notes on why it is
better than the sector cap I proposed in F-13: correlation is **measured from
returns** rather than assumed from a sector label (semis and industrials can
be one factor in practice), and the volatility ladder makes position size
respond to the risk actually present rather than to a fixed 10% cap. Hawkeye
already computes ATR and has 365 days of bars per name, so the inputs are in
hand.

Keep Hawkeye's fixed-fractional risk-to-stop as the *primary* sizing rule and
apply this as a **cap**, so the two disciplines compose rather than compete.

**Fits:** deterministic, pre-registered numbers, immune to narrative — this is
`config.py` material and belongs in the Risk Officer, which already holds veto
power.

## S-2. Portfolio heat, constraint hierarchy, circuit breaker
**Source: `position-sizer` and `drawdown-circuit-breaker` (claude-trading-skills)**

Three patterns, all missing from Hawkeye:

**Portfolio heat** — total open risk capped at 6–8% of equity. Hawkeye's
8 positions × 0.75% implies 6%, but never computes it and never re-computes it
as stops move.

**Constraint hierarchy** — when position %, sector %, and heat caps all apply,
the **tightest binding constraint** decides the size, and the report says
*which one bound*. `build_position_plan()` applies caps sequentially
(`sizing.py:70-74`) and reports none. Reporting the binding constraint is what
makes "our sizing rule is wrong" a diagnosable claim.

**Drawdown circuit breaker** — a calendar-anchored ladder: daily −2% → HALTED
(resets next session) · 2 consecutive losing closes → COOLDOWN (24h) ·
weekly −5% → HALTED (resets Monday) · monthly −8% → HALTED (resets month
start). Every threshold auto-resets on a boundary, so it throttles without
requiring a judgment call.

Hawkeye has **no notion of a bad week**. For a system whose entire pitch is
mechanical enforcement of pre-registered limits, per-position discipline with
no portfolio-level governor is the largest structural hole in the risk model.

Worth quoting the source's own framing, because it is precisely Hawkeye's
stance: the breaker "is a recommendation and recordkeeping tool. It does not
replace human judgment, and it does not enforce broker-side blocks."

**Also worth borrowing — `Vibe-Trading`'s mandate with auto-expiry.** A risk
envelope (universe, size, exposure, daily cap) that must be **actively
renewed** rather than silently inherited turns "we never revisited the limits"
into a visible, dated event.

## S-3. Multi-factor PEAD setup scoring
**Source: `earnings-trade-analyzer` (claude-trading-skills)**

A five-factor weighted score (0–100, graded A/B/C/D) over: gap size,
**pre-earnings 20-day trend**, **volume ratio (20d / 60d)**, **position vs
MA200**, **position vs MA50**. Grade A → direct entry candidate; Grade B →
requires pullback confirmation before committing.

**Hawkeye today** (`scout/scout.py:58-77`): `score_candidate()` uses EPS
surprise + revenue surprise + an event-day gap band. No trend, no volume
confirmation, no moving-average context — despite `build_snapshot()` holding a
year of bars and already computing ADV20 and ATR14.

**Proposal.** Add the three missing factors to `snapshot.py` (a few lines
each, pure functions, offline-testable like everything else there) and into
the score. They serve double duty: the ranking improves, and the **Bull's
dossier gains real setup context** it currently lacks entirely.

The graded A/B/C output with different handling per grade is also worth
copying — it is strictly more informative than Hawkeye's single ranked list,
and it maps onto the `research_probe` third state proposed in G-6.

## S-4. Regime as an exposure mode, not a veto
**Source: `macro-regime-detector` + `exposure-coach` +
`pre-trade-discipline-gate` (claude-trading-skills)**

Six cross-asset ratios on monthly frequency, smoothed with 6/12-month MAs:
RSP/SPY (breadth, 25%), 10y−2y (20%), HYG/LQD (credit, 15%), IWM/SPY (size,
15%), SPY/TLT (15%), XLY/XLP (cyclical vs defensive, 10%). Five regimes:
Concentration / Broadening / Contraction / Inflationary / Transitional.

The important design decision is **how it is consumed**: the discipline gate
blocks entries when the exposure coach says `REDUCE_ONLY` or `CASH_PRIORITY`.
Regime is a **mode driving exposure**, not a per-trade veto — and the source
is explicit that regime is a 1–2 year strategic signal, so using it to gate
individual trades would be a category error.

**Hawkeye today**: verified — no regime concept anywhere. `macro_regime`
exists only as an item in the Adversary's attack taxonomy
(`prompts.py:209`), which means the Adversary is asked to reason about a
regime the system has never measured.

**Proposal.** Compute the regime from free Yahoo series (all six inputs are
ETFs the existing provider can already fetch), expose it as a sizing
multiplier with modes `NORMAL` / `REDUCE_ONLY` / `CASH_PRIORITY`, and put it
in the dossier so the Adversary's `macro_regime` attacks have something to
stand on. Start advisory; promote to a sizing input only when the ledger shows
it discriminates.

## S-5. Measure alpha, not raw return
**Source: TradingAgents — memory entries store `raw_return` *and*
`alpha_return`; `benchmark_ticker` is a first-class config key**

The most-copied design in this space treats benchmark-relative return as a
config-level concept, because raw return does not tell you whether a decision
was good.

**Hawkeye today**: `benchmark.py:22-31` computes raw price return;
`Outcome.pnl_pct` is raw; `classify_outcome()` splits the skill/luck quadrant
on raw P&L sign. Since BUY candidates are selected for *recent positive
catalysts*, the BUY cohort is systematically higher-beta than the reject pile
— the comparison is biased toward a positive verdict even with zero skill.

**Proposal.** Add `benchmark_ticker` to `config.py`, store the benchmark
series alongside each evaluation, and report excess return in `benchmark`,
`Outcome`, and the quadrant classification. This is the borrowing that most
directly protects the Phase 0 verdict (F-05, F-18).

## S-6. Signal postmortem with named failure modes
**Source: `signal-postmortem` (claude-trading-skills)**

Closed signals are classified into **true positive / false positive / missed
opportunity / regime mismatch**, measured over **dual horizons (5d and 20d)**,
feeding weight adjustments — but only after **20+ signals**, an explicit
statistical-validity floor. Systematic failures also generate a **skill
improvement backlog** entry with issue type, severity, and suggested action.

**Hawkeye today**: `classify_outcome()` gives a 2×2 on P&L sign × thesis
accuracy at a single horizon. There is no `regime_mismatch` bucket, so a
correct thesis drowned by a market drawdown is filed as luck. There is no
minimum-n before drawing conclusions. And the "improvement loop closed"
Phase 0 criterion (`ROADMAP.md:44`) has no artifact — it is satisfied by
someone remembering to make a commit.

**Proposal.** Adopt the 20-signal floor as a stated rule before any doctrine
revision; add dual-horizon measurement (Hawkeye's time stop is 45 days, so
20d/45d); and make the improvement backlog a real file so the Phase 0 exit
criterion has something to point at.

---

# Part 3 — Agent design

## G-1. "No LLM ever touches an arithmetic operator"
**Source: AlphaAnalyst — a Python modeler computes DCF and comparables using
`decimal.Decimal`; the LLM writes the memo, never the numbers**

This is the sharpest architectural principle found anywhere in the survey, and
it is the clean statement of a problem Hawkeye has.

**Hawkeye today**: the Risk Officer is deterministic (good), but its inputs
are not. The **Bull authors the scenario price targets**, and those numbers
flow straight into `expected_value_pct()` and the RR hurdle that decide
approval (`pipeline.py:195`, `sizing.py:56-61`). The doctrine says "if the
Bull's numbers don't clear the bar, the Bull's prose cannot save the trade" —
true, but the converse is unguarded: **the Bull's numbers can save the trade**,
and the only thing standing between a marginal setup and a BUY is the Bull's
choice of a bull-case target. F-01 shows the pressure is not hypothetical:
under current rules, only a target table that violates the doctrine's own base
rates can be approved.

**Proposal, in ascending order of ambition.**

1. **Derive candidate targets deterministically** and let the Bull *select and
   justify* rather than invent: ATR-scaled bands, the 52-week high, the
   pre-event level, an analyst-estimate-implied level. The LLM's job becomes
   choosing among computed levels with a written rationale — the thing LLMs
   are actually good at — instead of producing the number that scores itself.
2. **Cross-check against the option-implied distribution** (F-11). The options
   chain gives an independent, market-priced range for the same horizon. A
   bull target far outside it is either the edge or the tell; either way it is
   mechanically checkable.
3. **Constrain scenario tables against the doctrine's own base rates in code.**
   The base rates are already written in `_SHARED_DOCTRINE` as instructions to
   the Bull. Invariant 3 says code must enforce what prompts request — right
   now, nothing does.

**Fits:** this is invariant 3 applied to the one place it currently is not.

## G-2. Citation tagging with mechanical validation
**Source: AlphaAnalyst — every numerical claim carries a `[F1]`-style tag
resolving to `memo.citations`, each anchored to a real source (filing CIK,
URL, timestamp); figures cross-checked against the 10-K within 5% tolerance;
sections whose numbers lack tags are downgraded; the eval suite exits non-zero
if fabricated citations > 0**

Their framing: **"the LLM is a writer, not a knower."**

**Hawkeye today**: `Claim.verification` and `Attack.evidence` are free-text
strings (`contracts/models.py:141,203`). Nothing links an assertion to a
dossier field. The Bull is told "you may not invent facts"
(`prompts.py:167-168`) and the Judge "must not introduce new facts"
(`prompts.py:227-228`) — both rules are prompt-only, with no mechanical check
whatsoever. For a project whose stated principle is "code enforces what prompts
request", this is the largest unenforced instruction in the system.

**Proposal.** Add a `source_ref` to every quantitative assertion — a JSON path
into the dossier the role was given (`snapshot.atr_pct_14d`,
`catalyst.event_date`, `news[2].headline`, `gate_report.results[3]`). Validate
in the existing parsers (`parse_thesis`, `parse_attack_report`), which already
own all normalization and are the natural enforcement point:

- a `source_ref` that does not resolve → reject the payload, exactly as a
  malformed schema is rejected today (`casefile.py:162-179` already fails
  before anything reaches the ledger)
- a numeric assertion with no `source_ref` → mark the claim `unsourced` and
  exclude it from EV/RR inputs, mirroring AlphaAnalyst's "downgrade untagged
  sections"

This turns "you may not invent facts" from an instruction into a property, and
it is cheap because the plumbing already exists.

## G-3. Cross-family adversary
**Source: AlphaAnalyst — Claude Opus synthesizes, GPT-4o plays Devil's
Advocate, "deliberately chosen from a different model family… ensures the
challenger isn't echoing the primary agent's biases". Corroborated by orallexa
(Haiku debaters, Sonnet judge) and by the deliberation literature (~60%
correlated errors between same-model agents; the Metaculus study found
deliberation helped only with diverse models).**

AlphaAnalyst's formulation is worth keeping verbatim: both agents see
identical source material but operate under **opposite incentive structures** —
synthesis aims for coherence, advocacy aims for contradiction.

**Hawkeye today**: one vendor, one model, all three roles
(`VERIFICATION_PROTOCOL.md:86-87` acknowledges this; `ROADMAP.md:71-72` defers
it to Phase 2 "when verdict patterns suggest same-model correlation").

**Proposal.** Move it up. The research says the correlation is ~60% *a priori*,
so waiting for verdict patterns to reveal it is waiting for evidence we already
have. A-4 makes it a config change; in session mode the Agent tool takes a
`model` parameter per subagent, so the cost is close to zero.

Hawkeye should go one step further than AlphaAnalyst: give the Adversary not
just a different model but **different evidence** (F-07a), since the
information-asymmetry result says that is where most of the gain lives.

## G-4. Two-level adjudication and a three-way risk debate
**Source: TradingAgents — `graph/conditional_logic.py`**

Their routing has two distinct adjudicated debates:

- **Bull ↔ Bear researchers** alternate until `count >= 2 × max_debate_rounds`,
  then control passes to a **Research Manager** who adjudicates.
- **Aggressive / Conservative / Neutral risk analysts** rotate until
  `count >= 3 × max_risk_discuss_rounds`, then a **Portfolio Manager**
  adjudicates.

So the *investment* question and the *sizing* question each get their own
adversarial process and their own judge. Defaults are 1 round each.

**Hawkeye today**: the investment question gets a full adversarial process
(Bull → Adversary → Judge). The sizing question gets a deterministic
calculator with no adversary at all.

**Proposal — deliberately partial.** Do **not** import the three-way LLM risk
debate; a deterministic Risk Officer immune to narrative pressure is one of
Hawkeye's genuine strengths and swapping it for three more LLM voices would be
a downgrade.

What *is* worth taking is the observation that **sizing deserves its own
adversarial input**, supplied deterministically: the volatility/correlation
limits of S-1, the portfolio heat and circuit breaker of S-2, and the regime
mode of S-4 are exactly the "conservative" voice, expressed as numbers. That
gets the structural benefit of their second debate without adding a single
LLM call.

Also note the single-round default. Hawkeye runs exactly one pass per role and
has sometimes been described as a limitation; the most-starred project in the
field defaults to the same, and the martingale result explains why more rounds
of identical evidence do not help.

## G-5. Memory that stores outcomes, and message hygiene
**Source: TradingAgents — `agents/utils/memory.py` + the `Msg Clear` node in
`conditional_logic.py`**

Their memory is an append-only markdown log in two phases: **Phase A** records
ticker, date, rationale and rating tagged `pending`; **Phase B**, once results
are known, backfills `raw_return`, `alpha_return`, `holding_days` and appends a
reflection. Retrieval injects up to **5 same-ticker** entries plus **3
cross-ticker** lessons into the next prompt.

Separately, the graph has an explicit **`Msg Clear`** step between analyst
stages — deliberate context hygiene so stages do not bleed.

**Hawkeye today**: the ledger is far richer than their markdown log — and is
never read back into any prompt. The calibration table is a report for the
human, not an input to the next decision. Meanwhile Hawkeye's roles are
stateless separate calls, which *already is* `Msg Clear`, done better and by
construction.

**Proposal.** Hawkeye has the superior memory substrate and none of the
retrieval. Close that loop (F-08): inject the Bull's own calibration band
statistics into its prompt, and give the Adversary its resolved kill-shot
record. Two cautions the ledger's design should impose that theirs does not:

- retrieve on **setup similarity**, not just same-ticker recency — Hawkeye
  trades catalysts, so "last five earnings-drift setups with a 5–15% gap" is
  the relevant reference class, and `catalyst.type` + the S-3 factors make it
  computable
- keep injection **one-directional and pre-registered**: what gets injected is
  part of the prompt version recorded in the run manifest (A-5), so a
  calibration shift is attributable to a known change

## G-6. Fail-closed with a third verdict state
**Source: `edge-pipeline-orchestrator` (unresolved REVISE verdicts after max
iterations downgrade to `research_probe` rather than being forced to export;
strict mode converts warnings into REVISE) and `pre-trade-discipline-gate`
(outputs `GO` / `NO_GO` / **`REVIEW_REQUIRED`** / `NO_ACTIONABLE_ORDERS`)**

Both deliberately refuse a binary. `REVIEW_REQUIRED` specifically means
"upstream artifacts were missing or unreadable" — which is *not* the same as
"this failed a rule".

**Hawkeye today**: `DecisionType` is `BUY | PASS`. Everything that is not a
BUY becomes a PASS: a genuine reasoned rejection, a rule-enforcement
overturn (F-02), a risk veto, a hard gate failure, and an enrichment outage
all collapse into the same label. `cohort_of()` recovers only a partial split
(`benchmark.py:34-39`) by checking whether a thesis exists.

This matters for the experiment, not just for tidiness: "the Judge rejected
this on the merits" and "the Judge's phrasing failed a substring match" are
different events, and Phase 0 is trying to measure the first.

**Proposal.** Add a third status — `INSUFFICIENT_RECORD` — for outcomes where
the system could not reach a judgment: unresolvable data, a failed validation,
an unverified hard gate (F-03), a rule-enforcement overturn. Exclude it from
the BUY/PASS cohort statistics and count it separately as a **process health
metric**, which is exactly what `ROADMAP.md:82-83` says the weekly review is
for. Adopt `strict` mode too: a flag that promotes warnings to blocks, useful
for exactly the "are the gates too loose?" question the weekly review asks.

## G-7. Structured gate inputs, never prose
**Source: `pre-trade-discipline-gate` — the gate consumes a JSON/YAML checklist
of candidate answers (entry confirmed? stop predefined? size matches plan?
actual risk ≤ planned risk?) and reads upstream state from other skills'
artifacts, not from narrative**

**Hawkeye today** (`pipeline.py:139-145`): the severe-attack rule matches
`attack.statement[:60]` as a substring of concatenated prose. Measured in
F-02: a faithful paraphrase, or one added character, silently overturns a BUY.

The borrowed lesson is general and worth stating as a rule for the codebase:
**enforcement must read identifiers and structured fields, never model prose.**
Give each `Attack` an id, put ids in the Judge's input, require
`addressed[].attack_id` in the schema. `new_id()` already exists; the schemas
are already `additionalProperties: false` with explicit `required` lists, so a
missing id becomes impossible rather than undetectable.

## G-8. Per-source accuracy ledger with dynamic weights
**Source: orallexa — `source_accuracy.py` (JSONL, per-source forward returns
backfilled nightly), `dynamic_weights.py` (rolling ~20-day accuracy scales each
source's contribution), `bias_tracker.py` (rolling Brier; when confidence
consistently exceeds accuracy, next cycle's confidence is dampened
multiplicatively)**

A 59-star project has the closed calibration loop that the 95k-star project
does not — and that Hawkeye, whose entire thesis is calibration, also does not.

**Hawkeye today**: Brier is computed and displayed. Nothing consumes it.

**Proposal.** Hawkeye's version should be *better* than orallexa's, because
Hawkeye tracks claims rather than opaque sources: apply the dampening per
**probability band** using the existing `calibration_table()` bands, not as a
single global multiplier. Record raw and recalibrated probabilities separately
so invariant 1 holds — pre-registration is preserved as long as the raw value
is immutable and the recalibrated one is a derived journal artifact.

Also worth copying: orallexa gates real money behind **written Brier
improvement proof** rather than sentiment, and publishes its honest
walk-forward table (1 strong pass / 7 pass / 33 marginal) instead of claiming
victory. That posture is more Hawkeye-like than most of what the survey found.

---

# Mapping: Hawkeye issue → borrowed design

| Hawkeye issue | Source project | Borrowed design | Item |
|---|---|---|---|
| Provider can't grow to new data types | OpenBB | QueryParams + Data pair per type | A-1 |
| Data source policy is prose | TradingAgents | vendor-per-category config | A-2 |
| Outage silently biases the funnel | Vibe-Trading | fallback chain + cache; outage ≠ rejection | A-3 |
| No seam for per-role model/effort | TradingAgents, orallexa | deep/quick think + effort as config | A-4 |
| Can't diagnose a calibration regime break | edge-pipeline-orchestrator | run manifest + prompt hashes + dry-run | A-5 |
| Session can reach ledger write path | maverick-mcp | read-only MCP surface, writes CLI-only | A-6 |
| Weekly/monthly rituals have no artifact | claude-trading-skills | `workflows/` manifests | A-7 |
| No correlation or concentration control | ai-hedge-fund | vol-scaled limit × correlation multiplier | S-1 |
| No portfolio governor | claude-trading-skills | heat, tightest-binding-constraint, breaker | S-2 |
| Scout score lacks setup context | earnings-trade-analyzer | trend + volume + MA50/200 factors | S-3 |
| No regime awareness | macro-regime-detector | 6 cross-asset ratios → exposure mode | S-4 |
| Cohort verdict measures beta (F-05) | TradingAgents | `benchmark_ticker`, store `alpha_return` | S-5 |
| Attribution has no regime bucket (F-18) | signal-postmortem | regime_mismatch + 20-signal floor | S-6 |
| Bull's own numbers gate the trade (F-01) | AlphaAnalyst | LLM never touches arithmetic | G-1 |
| "May not invent facts" is unenforced | AlphaAnalyst | source_ref validated in parsers | G-2 |
| Single model plays all roles | AlphaAnalyst, orallexa | cross-family adversary | G-3 |
| Sizing has no adversarial input | TradingAgents | second adjudicated stage — deterministic here | G-4 |
| Ledger never feeds the next decision | TradingAgents | outcome memory retrieved into prompts | G-5 |
| Every non-BUY collapses to PASS (F-02) | edge-pipeline, discipline-gate | third state: INSUFFICIENT_RECORD | G-6 |
| Enforcement matches prose (F-02) | pre-trade-discipline-gate | structured ids, never narrative | G-7 |
| Brier is measured, never used (F-08) | orallexa | per-band recalibration + bias tracker | G-8 |

---

# Suggested order

Sequenced by *enabling* relationships, not by appeal. Several later items are
blocked on earlier ones.

**First — small refactors that unblock everything else**
1. **A-4** per-role model + effort config → unblocks G-3 and the F-08 experiment
2. **G-7** structured attack ids → stops silent BUY loss (F-02)
3. **G-6** `INSUFFICIENT_RECORD` third state → makes funnel stats mean something
4. **S-5** `benchmark_ticker` + alpha → makes the Phase 0 verdict mean something

**Second — design changes with the highest measured payoff**
5. **G-3** cross-family adversary (config change once A-4 lands)
6. **G-8** per-band recalibration + calibration injected into the Bull (G-5)
7. **G-2** `source_ref` validation in the existing parsers
8. **A-1 / A-2** provider standardization + vendor routing → unblocks F-10 data

**Third — risk architecture, in dependency order**
9. **S-1** volatility + correlation limits (needs nothing new; bars are in hand)
10. **S-2** portfolio heat → constraint hierarchy → circuit breaker
11. **G-1** deterministic target derivation + option-implied cross-check

**Fourth — when the ledger justifies it**
12. **S-3** setup factors · **S-4** regime mode · **S-6** postmortem buckets
13. **A-5** run manifest · **A-6** MCP read surface · **A-7** workflow manifests

## What not to borrow

- **Investor-persona agents** (ai-hedge-fund's 14 named investors). Take its
  `risk_manager.py`; leave the personas. Unfalsifiable by construction and
  incompatible with scoring our own calibration.
- **The three-way LLM risk debate** (TradingAgents). Hawkeye's deterministic
  Risk Officer is stronger than three more correlated LLM voices. Take the
  structural insight (sizing deserves its own adversary), supply it with
  numbers.
- **Broker connectors and execution paths** (Vibe-Trading, TradingAgents).
  Invariant 5. Their *guardrails* — mandate, auto-expiry, fail-closed
  pre-trade assessment — are worth copying; the order path is not.
- **Reported backtest numbers.** TradingAgents reports Sharpe ≥ 5.60 on
  AAPL/GOOGL/AMZN. Borrow the architecture; treat the number as unevidenced.
  A Sharpe above 5 on single-name equity points to a short window, look-ahead,
  or both — which is the strongest possible argument for Hawkeye measuring its
  own forward ledger instead of trusting anyone's backtest.
