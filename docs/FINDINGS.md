# Findings: what the landscape survey means for Hawkeye

Companion to `docs/LANDSCAPE.md`. That document catalogued what exists; this
one asks the only question that matters: **which of Hawkeye's current problems
does that knowledge actually solve?**

Method: read the codebase at `0c439b0`, form hypotheses about weaknesses,
then *verify each one by running it* rather than by inspection. Every number
below was measured, not estimated, against the real code:

```bash
.venv/bin/python docs/probes/findings_probes.py     # F-02, F-01, F-14, F-04
.venv/bin/python docs/probes/rr_ev_feasibility.py   # F-01 in full
```

These are diagnostics, not tests — they are deliberately outside `pytest` so
that fixing a finding does not "break" a suite that was asserting the bug.
Re-run them after any fix; the tables below should change.

Severity: **P0** = invalidates the Phase 0 experiment · **P1** = materially
degrades decisions or metrics · **P2** = worth doing when the ledger justifies it.

---

## Summary of the verdict

The survey's headline conclusion stands: nobody else does pre-registration,
hash-chained immutability, Brier calibration, or skill-vs-luck attribution.
That moat is real.

But the survey also surfaced a body of 2026 deliberation research that says
something uncomfortable and important: **Hawkeye's information separation is
the theoretically correct design, and Hawkeye is currently getting almost none
of its benefit**, because separating *stages* is not the same as separating
*evidence*. That is finding F-07, and it is the most valuable thing in this
document.

Separately, verification turned up a **P0 internal contradiction between the
doctrine's base rates and its own risk hurdles** (F-01) that would have
produced a meaningless Phase 0 verdict. That one is not from the survey — it
came out of testing the code — but it blocks everything else, so it leads.

---

# 1. Blocking defects — fix before the Phase 0 verdict means anything

## F-01 (P0) — The risk hurdles and the doctrine's base rates are mutually inconsistent

**Where:** `hawkeye/risk/sizing.py:56-61`, `hawkeye/tribunal/pipeline.py:152-160`,
`hawkeye/config.py:28-29`.

`_stop_and_target()` measures reward/risk against the **base-case** scenario
target. `build_position_plan()` then demands `reward_risk >= 2.0` *and*
`expected_value_pct >= 5.0`. Measured consequence — minimum base-case gain
required to clear RR ≥ 2, by stop distance:

| stop distance | required base-case gain |
|---|---|
| 4% | +8% |
| 8% (doctrine fallback) | **+16%** |
| 12% | +24% |
| 20% (veto boundary) | +40% |

Now compare against what `_SHARED_DOCTRINE` (`prompts.py:150-164`) *orders the
Bull to believe*: PEAD drift is "low single-digit percent over 1-3 months",
and "most single-name catalyst trades resolve within ±15% over 4-8 weeks".

Four scenario tables run through the real risk officer:

| Scenario table (entry 100) | EV | RR | Result |
|---|---|---|---|
| A. Doctrine-faithful PEAD: stop −8%, targets 90/104/112, p .40/.40/.20 | +0.00% | 0.50 | **VETOED** |
| B. Optimistic but inside ±15%: stop −8%, targets 92/108/115, p .30/.45/.25 | +4.95% | 1.00 | **VETOED** |
| C. Tight 4% stop, same targets | +4.95% | 2.00 | **VETOED** (EV misses by 0.05pp) |
| D. stop −8%, targets 92/**116**/**130**, p .30/.45/.25 | +12.30% | 2.00 | APPROVED |

Only D passes — and D's base case is +16% with a bull case of +30%, i.e. the
bull target sits **outside the ±15% band the doctrine calls typical**. A
doctrine-faithful thesis is structurally un-buyable.

So the system has exactly two reachable states: pass on everything, or buy
only theses whose scenario tables violate the base rates the Bull was
instructed to respect. Neither produces a meaningful reading on the Phase 0
exit criterion "BUY rate of gate-passing candidates in 10–40%", and the
project-level kill criterion would fire on an artifact of arithmetic rather
than on evidence about the strategy.

**Root cause** is definitional, not numerical. Conventionally, reward/risk is
measured to *the target you intend to exit at*, while expectancy is the
probability-weighted check. Hawkeye measures RR to the probability-weighted
*median* case and then applies a second expectancy hurdle — conservatism
counted twice.

**Recommended fix (one config + one function):** measure RR to the bull-case
(or an explicit exit target) and keep `min_expected_value_pct = 5.0` as the
honesty check on the full distribution. This restores each hurdle's
conventional meaning and leaves the Bull unable to buy its way past EV by
inflating one tail (inflating the bull target raises the required RR
numerator but also raises EV — both are checked). If instead you prefer to
keep RR on the base case, `min_reward_risk` must come down to ≈1.2–1.5 and
the doctrine should say explicitly that it is a median-case ratio.

Either way this is a doctrine change: a `config.py` diff with rationale in the
commit message, per CLAUDE.md invariant 7. **Do not tune it to make BUYs
appear** — pick the definition that is coherent, then let the ledger judge.

## F-02 (P0) — `_judge_rule_check` overturns BUYs on paraphrase

**Where:** `hawkeye/tribunal/pipeline.py:139-145`.

Enforcement matches attacks by substring: `attack.statement[:60].lower()` must
appear verbatim inside the concatenated `addressed` statements. Measured
against a severity-5 attack with a genuine, substantive rebuttal:

| Judge's `attack_statement` | Result |
|---|---|
| verbatim echo of the attack | BUY survives |
| faithful paraphrase + full rebuttal | **BUY overturned to PASS** |
| identical except one added `~` character | **BUY overturned to PASS** |

The rule that is supposed to enforce "every severe attack is answered" instead
enforces "the Judge copy-pastes strings". Every paraphrase becomes a silent
false PASS, contaminating the BUY-rate and cohort statistics that Phase 0
exists to measure — and it fails *quietly*, appended to the rationale where it
reads like a real judgment.

**Fix:** stop matching on prose. Give each `Attack` a stable `id` (the codebase
already has `new_id()`), put the ids in the Judge's input, and require
`addressed[].attack_id`. Index-based matching is exact, and a schema-level
`required` field makes omission impossible rather than undetectable. Keep the
free-text statement for the human report.

This is the same lesson `tradermonty/claude-trading-skills` encodes in its
`pre-trade-discipline-gate`: gates take **structured checklist answers**, not
prose, and emit `GO` / `NO_GO` / `REVIEW_REQUIRED` — the third state matters,
because "the artifact was unreadable" is not the same as "the trade failed a
rule". Hawkeye currently collapses both into PASS.

## F-03 (P1) — Hard gates pass on missing data, contradicting invariant 6

**Where:** `hawkeye/gates/entry_gates.py:17-30`, `contracts/models.py:106-107`.

`_minimum`/`_maximum` return `passed=True, unverified=True` when a value is
`None`, and `hard_failures` only counts `hard and not passed`. So a candidate
with unknown market cap or unknown ADV **clears the hard liquidity gates** and
proceeds to spend LLM budget. CLAUDE.md invariant 6 says "missing data is
`unverified`, never a silent pass". It is flagged, but it does pass — the flag
is decoration on a gate that opened.

**Fix:** an unverified *hard* gate should block by default, with an explicit
`--allow-unverified` override recorded as a journal event. Soft gates can keep
today's behaviour. `trader-memory-core`'s registration rule is the same
posture: **fail-closed**, only explicitly-passing verdicts get recorded.

---

# 2. Measurement integrity — protecting the moat

## F-04 (P0) — The Brier score is computed on a self-selected sample

**Where:** `hawkeye/ledger/store.py:246-257`, `cli.py:456-461`.

`all_resolved_claims()` returns only claims that have a `claim_resolution`
event. Resolution is manual (`hawkeye resolve-claim`). Unresolved claims are
silently excluded — not counted as anything, just absent.

Measured, on one thesis with five claims where two came true:

| What gets resolved | Brier |
|---|---|
| all five claims | **0.295** |
| only the two that came true | **0.031** |

A busy user who resolves the memorable claims and forgets the awkward ones
reports a near-perfect Brier score. The Phase 0 gate "≥50 resolved claims,
overall Brier < 0.25" can be satisfied by selection alone. This is the
project's single most important metric and it is currently gameable by
omission — the exact failure mode the ledger was built to prevent, reappearing
one level up.

**Fix:** make non-resolution visible and costly.
1. Track the denominator: report `resolved / due` alongside every Brier figure;
   a calibration table with a 60% resolution rate is not a result.
2. The sentinel already emits `claim_due` (`sentinel/monitor.py:78-85`) — add
   an overdue escalation and block `hawkeye outcome` from producing a quadrant
   while claims are past due and unresolved.
3. Add a resolution outcome of `unresolvable` recorded as a journal event, so
   "we couldn't check this" is a first-class datum rather than a silent gap.
   (`signal-postmortem` does the equivalent with its `regime_mismatch`
   category: a named bucket beats a missing row.)

## F-05 (P1) — Cohort benchmark measures beta, not skill

**Where:** `hawkeye/scout/benchmark.py:22-31`, `cli.py:249-297`.

`forward_return()` computes a raw price return; `cohort_stats()` compares raw
means; the CLI declares success on `spread > 0`. No market adjustment. A BUY
cohort at +5% while SPY did +6% prints as a win. Since BUY candidates are
selected for *recent positive catalysts*, the cohort is systematically higher
beta than the reject pile, so the comparison is biased in favour of a positive
verdict even with zero selection skill.

TradingAgents' memory layer — the most-copied design in this space — stores
`raw_return` **and `alpha_return`** per decision, precisely because raw return
does not tell you whether the decision was good.

**Fix:** store a benchmark series (SPY via the existing Yahoo provider) and
report excess return over the identical window. Sector-relative is better
still and the `sector` field already exists on `CandidateBrief` (unused —
see F-11).

## F-06 (P1) — No statistical bar on the primary viability metric

**Where:** `cli.py:290-294`, `docs/ROADMAP.md:42`.

The Phase 0 exit test is "BUY mean return > tribunal-PASS mean return" — a
comparison of two small-sample means with no confidence interval, no
significance test, and no minimum n per cohort. With ~50 evaluations split
three ways and single-name equity variance, that comparison is close to a coin
flip. The project's own doctrine forbids exactly this kind of inference
elsewhere ("small-n P&L is noise") but its primary gate commits it.

The wider practice is well-settled: ≥30 observations before any inference,
percentile bootstrap CIs (resample trades, read the 2.5th/97.5th percentiles)
because they need no distributional assumption, and a multiplicity correction
once you have tested several configurations. `orallexa` — a 59-star project —
nonetheless pre-registers a harder bar than Hawkeye does: **PASS = OOS Sharpe
≥ 0.60 AND p < 0.05**, MARGINAL in between, and it published a result of
1 strong pass / 7 pass / 33 marginal rather than claiming victory.

**Fix:** replace the bare mean comparison with a bootstrap CI on the
BUY−PASS spread and pre-register the decision rule now, before data exists:
e.g. *promote* if the 95% CI lower bound > 0; *kill* if the upper bound < 0;
*extend the sample* otherwise. Pre-registering the statistic is the same
discipline the system already applies to theses — it should apply to itself.

## F-07 (P0 for design) — Information separation is procedural, not informational

**Where:** `hawkeye/tribunal/prompts.py:266-294`, `tribunal/casefile.py:121-159`.

This is the most important finding in the document.

Hawkeye separates roles by **stage**: the Bull cannot see attacks, the
Adversary sees the thesis, the Judge sees the record. But `_brief_dict()`
hands *every role the identical dossier*. No role can introduce a fact another
role does not have.

The 2026 literature is now specific about what that costs:

- Symmetric multi-agent debate is a **martingale** — expected correctness does
  not improve across rounds when agents receive identical inputs; closed
  deliberation forms a Markov chain, so mutual information with the ground
  truth can only *decrease*. LLM errors are 60%+ correlated, so ensembling has
  a hard error floor.
- Agents conform to perceived majority, producing **wrong consensus held with
  high confidence** — calibration failure, which is precisely what Hawkeye
  measures itself on.
- The fix is **designed information asymmetry**: partition evidence into a
  shared public set plus *disjoint private* sets, so each agent holds
  something that can only reach the others through deliberation. Instantiated
  as InfoDelphi and evaluated on 375 real prediction-market questions, this
  gave **12–18% better Brier and 4–8pp better accuracy** than the strongest
  single- and multi-agent baselines — and **removing the asymmetry eliminated
  most of the gain**.
- A separate study on 202 Metaculus questions found deliberation helped only
  with **diverse models**; homogeneous groups got nothing (log loss −0.020,
  ~4%, p=0.017, in the diverse-model condition).

Read together: Hawkeye picked the right architecture and then supplied it with
symmetric evidence and a single model, which is the configuration the research
says yields the least benefit. The good news is that Hawkeye is unusually well
placed to exploit the fix — session mode makes both changes nearly free.

**Fix, in two independent parts.**

*(a) Give the Adversary its own evidence.* Keep the Bull on the current
dossier. Give the Adversary the same dossier **plus a private disconfirmation
packet** it alone sees: short volume trend, options-implied move and put/call
skew, insider Form 4 activity, filing full-text hits on risk language. All of
this is free and zero-auth via `global-stock-data` (F-09). The Judge sees the
union, as today.

This is a strict strengthening of invariant 4, not a relaxation: the asymmetry
runs *in favour of falsification*, which is the direction the doctrine already
wants. It also fixes a live inconsistency with invariant 3 — the Adversary is
told to attack on `crowding_positioning`, `liquidity`, and `macro_regime`
(`prompts.py:207-210`) while the system supplies **no data on any of them** and
enforces nothing mechanically. Today those attack categories can only be
speculation.

*(b) Run the roles on different models.* `casefile.write_package()` already
isolates each role; the session driver spawns a fresh subagent per role and
the Agent tool takes a `model` parameter. Cross-model roles are a
configuration change, not an architecture change. `orallexa` already does the
cheap version of this (Haiku for Bull/Bear, Sonnet at high effort for the
Judge) at ~$0.003 per analysis. Hawkeye's Phase 2 lists role independence as
"when verdict patterns suggest same-model correlation" — the research says the
correlation is ~60% a priori and the fix is nearly free, so this should move up.

## F-08 (P1) — The Bull is Brier-scored, and nothing corrects its known overconfidence

**Where:** `hawkeye/tribunal/prompts.py:178-181`, `ledger/scoring.py:51-65`.

The Bull states probabilities; the ledger scores them; nothing closes the loop.
The calibration table is a report the human reads, never an input to the next
decision.

What is now known about the raw material:

- **KalshiBench** (300 post-cutoff prediction-market questions) finds
  systematic overconfidence in *every* frontier model. Best-calibrated was
  Claude Opus 4.5 at ECE = 0.120. At **>90% stated confidence, models were
  wrong 27% of the time.**
- Extended reasoning made it **worse**: GPT-5.2-XHigh scored ECE = 0.395 at
  comparable accuracy, consistent with long reasoning chains reinforcing an
  initial hypothesis rather than updating on evidence.

Two consequences.

First, the Phase 0 bar "overall Brier < 0.25" is being asked of a component
that is known to arrive miscalibrated. Hawkeye should expect to *earn* that
number through a recalibration layer, not hope the model has it natively.

Second, and sharper: **CLAUDE.md specifies "adaptive thinking" for the LLM
client.** The KalshiBench result says more thinking may degrade exactly the
quantity Hawkeye scores itself on. Hawkeye is one of very few systems on earth
with the machinery to test this properly — pre-registered probabilities,
resolution, Brier, an immutable ledger. **Run thinking-budget as a pre-registered
A/B against Brier.** If more thinking hurts calibration, that is a genuine
finding, cheap to produce here and expensive anywhere else.

**Fix:**
1. **Feed the calibration table back into the Bull's prompt.** "In the 0.70–0.85
   band you have historically stated 0.78 and resolved true 0.55" is the
   closed loop the whole ledger was built to enable, and no surveyed project
   has it. `orallexa`'s `bias_tracker` implements the naive version —
   multiplicative dampening when rolling confidence exceeds rolling accuracy
   ("if I said 80% ten times and got 6 right, report ~60%") — which is worth
   copying as a v1.
2. **Record raw and recalibrated probabilities separately.** Pre-registration
   is preserved as long as the raw value is immutable; the recalibrated value
   is a derived journal artifact. Never overwrite (invariant 1).
3. **Elicit in frequency format.** Natural frequencies ("in 100 comparable
   post-beat setups, this happens in N") measurably beat percentages for
   base-rate reasoning, and explicitly elicited probabilities calibrate far
   better than logprobs (0.12 vs 0.27 bin-level error). This is a prompt edit
   with plausible upside and no cost.
4. Gate feedback on sample size — `signal-postmortem` requires **20+ signals**
   before adjusting a weight. Adopt the same floor so the loop does not chase
   noise.

## F-09 (P1) — The Judge's conviction is never scored

**Where:** `docs/VERIFICATION_PROTOCOL.md:13`, `ledger/scoring.py`, `cli.py:493-516`.

The protocol table states the Judge is scored on "calibration of `conviction`",
and `JUDGE_SYSTEM` tells it so ("You are scored on it", `prompts.py:243`). No
code does this. `calibration_table()` consumes claim probabilities only;
`Verdict.conviction` is never resolved against any outcome. The Adversary's
stated incentive ("kill-shots that later prove correct") is likewise
unimplemented — that one is at least acknowledged as a known limitation
(`VERIFICATION_PROTOCOL.md:88-89`); the Judge's is not.

Telling an agent it is being scored on something that is never scored is not a
harmless white lie: it is the one incentive claim in the system that cannot
come true, and the protocol document asserts it as fact.

**Fix (cheap):** conviction is a probability that the trade beats its base
case — already resolvable from data the ledger holds at `outcome` time. Score
it in the same Brier machinery, report it as a separate row in
`hawkeye calibration`. Until then, correct `VERIFICATION_PROTOCOL.md` to list
it under known limitations.

---

# 3. Data layer

## F-10 (P1) — `global-stock-data` closes all four named gaps, free and zero-auth

**Where:** `docs/DATA_SOURCES.md:27-29` lists four roadmap gaps: institutional
positioning, short interest, options flow, consensus estimate revisions.

`simonlin1212/global-stock-data` (1.4k★, Apache-2.0, `SKILL.md` + Python,
`requests` only, no API keys) covers **all four**:

| Hawkeye gap | Function | Source tier |
|---|---|---|
| consensus estimate revisions | `analyst_estimates(symbol)` — EPS forecasts, rating trends, upgrades/downgrades | C (personal) |
| short interest | `short_volume_symbol(symbol, days, market)` — FINRA Reg SHO | B |
| options flow | `options_chain_cboe(ticker)` with full Greeks + IV; `chain_summary()` → put/call, VW IV, net delta | C (personal) |
| institutional positioning | `institutional_holders(symbol)`; `daily_filings(date, forms)` → Form 4/13F/8-K stream | S / C |

Estimate revisions deserve emphasis: the doctrine's `underreaction` edge is
defined as "analysts and flows reprice slowly" (`INVESTMENT_DOCTRINE.md:35`),
and revision direction is the *mechanism* of that edge — yet the system
currently has no revision data at all. This is the highest-value single
addition to the dossier.

Also directly useful: `sec_xbrl_facts(cik, metrics)` for the `data_integrity`
attack category (one-offs, accounting quirks — currently unevidenced),
`fulltext_search()` over filings, `treasury_yield_curve(year)` for F-13, and
`earnings_calendar(date)` from Nasdaq as a second source alongside Finnhub.

**Caveats to honour:** tier C (CBOE, Yahoo) is **personal research only** —
fine for Hawkeye today, but record the tier per field so a future change of use
is a visible decision, not an accident. SEC requires a declared User-Agent and
10 req/s; FINRA is tier B (verify before any commercial use).

## F-11 (P1) — Option-implied move as a mechanical check on the Bull's scenarios

Follows from F-10 and directly mitigates the Goodhart pressure in F-01: the
Bull authors the scenario table that the risk officer then scores.

The options chain gives an independent, market-priced distribution for the same
horizon. A scenario table whose bull target sits far outside the option-implied
range is making a claim the market prices as very unlikely — that is either the
edge, or the tell. Either way it is mechanically checkable, which is exactly
what invariant 3 asks for, and it is the only defence proposed anywhere in this
document against a Bull that inflates targets to clear a hurdle.

Start as a soft gate surfaced to the Judge, promote to a veto only if the
ledger shows it discriminates. No surveyed project does this.

---

# 4. Risk and portfolio construction

## F-12 (P1) — Stop distance is unrelated to volatility

**Where:** `hawkeye/tribunal/pipeline.py:152-156`, `risk/sizing.py:52-54`.

The stop comes from the Bull's `price_below` kill criterion — `max(stops)`,
i.e. the *tightest* level the Bull wrote — with an 8% fallback, and only an
absolute 20% ceiling. `atr_pct_14d` is computed (`snapshot.py:27-39`), used as
a soft gate, and then **ignored by sizing**.

Combined with F-01 this is actively harmful: because RR is measured to the
base case, the cheapest way for a thesis to clear RR ≥ 2 is a tighter stop
(the table in F-01 shows a 4% stop needs only +8%, versus +16% at 8%). The
rules therefore reward stops placed *inside daily noise* — a 4% stop on a name
with 8% ATR will be hit by nothing at all.

**Fix:** floor the stop at a volatility multiple — `stop <= entry − k × ATR14`
with k ≈ 1.5–2.0 — and veto anything tighter. This is the standard ATR method
(`position-sizer`: "Stop placed at Entry − ATR × multiplier, typically 2.0x";
`orallexa` pairs Kelly with ATR adjustment). Add `stop_atr_multiple` to
`config.py` as a pre-registered doctrine number.

## F-13 (P1) — No portfolio-level risk controls at all

**Where:** `hawkeye/risk/sizing.py:62-63` — the only portfolio-aware check is
`open_position_count >= max_positions`.

Verified absent from the entire codebase: aggregate risk ("portfolio heat"),
sector concentration, correlation, drawdown circuit breaker. `sector` exists on
`CandidateBrief` and is **never read outside the model definition and the
market-data layer** — it appears in the Adversary's prompt taxonomy and nowhere
in enforcement.

So eight positions in the same sector, all sharing one factor, is a fully
compliant Hawkeye book. For a system whose entire pitch is mechanical
enforcement of pre-registered risk limits, per-position sizing with no
portfolio layer is the largest structural hole in the risk model.

**Fix, in the order the ledger will justify:**

1. **Portfolio heat** — cap total open risk (Σ per-position risk-to-stop) at a
   pre-registered ceiling. Standard practice is 6–8% of equity; Hawkeye's
   8 × 0.75% = 6% implies the limit already, but never computes or enforces
   it, and never re-computes it as stops move.
2. **Sector cap** — a maximum share of NAV per sector, using the field already
   carried on every brief.
3. **Constraint hierarchy** — apply position %, sector %, and heat caps as a
   set and let the **tightest binding constraint** determine size, reporting
   which one bound. `build_position_plan()` currently applies caps
   sequentially and reports none.
4. **Drawdown circuit breaker** — Hawkeye has no notion of a bad week. A
   concrete, pre-registered ladder from `drawdown-circuit-breaker`: daily −2%
   → HALTED (resets next session); 2 consecutive losing closes → COOLDOWN
   (24h); weekly −5% → HALTED (resets Monday); monthly −8% → HALTED (resets
   month start). Every threshold auto-resets on a calendar boundary, so it
   throttles without requiring a judgment call.

The circuit breaker fits Hawkeye's philosophy better than it fits its source:
these are pre-registered numbers, mechanically evaluated, immune to narrative —
`config.py` material, enforced in code. Note the honest framing in the
original: it "does not replace human judgment and does not enforce broker-side
blocks" — which is exactly Hawkeye's no-autonomous-execution stance (invariant 5).

Also worth borrowing from `Vibe-Trading`: a **mandate with an expiry date**.
A risk envelope that must be actively renewed rather than silently inherited
turns "we never revisited the limits" into a visible event.

---

# 5. Strategy and signal quality

## F-14 (P1) — The scout ranks by percentage surprise, which inverts the literature

**Where:** `hawkeye/scout/earnings.py:46-50, 61-82`, `scout/scout.py:58-77`.

`_surprise_pct()` is `(actual − estimate) / |estimate|`, `screen_events()` sorts
by it descending, and `run_scout()` enriches only the **top 15**
(`config.scout_max_enrich`). Measured on three synthetic prints:

| Ticker | estimate → actual | % surprise | absolute beat | rank |
|---|---|---|---|---|
| TINY | 0.01 → 0.03 | **+200%** | $0.02 | 1 |
| SMOL | 0.01 → 0.02 | **+100%** | $0.01 | 2 |
| MEGA | 2.20 → 2.60 | +18% | $0.40 | 3 |

The enrichment budget — and therefore the whole tribunal — is spent
denominator-first. Near-zero estimates manufacture enormous percentages, so
the shortlist systematically fills with the least informative prints, and the
$300M market-cap floor does not exclude them.

The PEAD literature is clear on the ordering, and Hawkeye currently has it
backwards:

1. **EAR** (earnings announcement abnormal return — the event-window reaction
   itself) is the **strongest** drift predictor, stronger than SUE.
2. **SUE** (surprise ÷ standard deviation of estimates) beats raw percentage,
   because dispersion tells you whether the surprise was genuinely unexpected:
   a beat against tight consensus is a real signal, the same beat against wide
   disagreement is not.
3. Raw percentage surprise is the weakest of the three, and it is the one
   Hawkeye ranks on.
4. Revenue surprise adds independent information on top of earnings surprise.

`score_candidate()` does already reward a confirming event-day gap
(2–15% → +15) — i.e. it half-knows about EAR — but only as a bonus applied
*after* percentage surprise has decided who makes the top 15. The ordering is
inverted at the stage that matters.

**Fix, in order of value per unit of work:**
1. **Rank on EAR first.** The event-day move is already computed
   (`snapshot.py:42-58`); make it market-adjusted (F-05 gives the benchmark
   series) and promote it to the primary ranking key.
2. **Replace raw % surprise with SUE** where `analyst_estimates()` (F-10)
   supplies dispersion; where it doesn't, at minimum scale the surprise by
   price rather than by estimate, which removes the zero-denominator blowup.
3. Screen *before* truncating to 15, on the better key.

## F-15 (P2) — No trend/volume context in the dossier

`earnings-trade-analyzer` scores PEAD setups on five factors: gap size,
**pre-earnings 20-day trend**, **volume ratio (20d/60d)**, and **position vs
MA50 / MA200**. Hawkeye's snapshot has bars in hand and computes none of the
last three.

All are a few lines in `snapshot.py`, all are unit-testable offline, and they
give both the ranking score and the Bull's dossier real setup context —
including a per-stock trend filter (price vs MA200) that needs no macro data.

## F-16 (P2) — No market regime awareness anywhere

Verified: no regime concept in the codebase. A long-only catalyst book with no
regime input takes the market's direction as an unhedged, unmeasured bet, and
PEAD behaves differently across regimes.

`macro-regime-detector` gives a concrete, cheap recipe — six cross-asset ratios
on monthly frequency: RSP/SPY (breadth), 10y−2y (yield curve), HYG/LQD
(credit), IWM/SPY (size), SPY/TLT (equity-bond), XLY/XLP (cyclical vs
defensive), smoothed on 6/12-month MAs. Every input is a free Yahoo series the
existing provider can already fetch.

Use it the way `pre-trade-discipline-gate` does — as a **mode**, not a veto:
`NORMAL` / `REDUCE_ONLY` / `CASH_PRIORITY`, driving a sizing multiplier. Note
the source treats regime as a 1–2 year strategic signal, so it should scale
exposure, not gate individual trades.

## F-17 (P2) — Outcome records no MAE/MFE

**Where:** `hawkeye/contracts/models.py:297-308`.

`Outcome` stores entry, exit, P&L, holding days. It cannot answer the question
that decides whether the stop rule is right: *how far did this trade go against
us before it worked?* `trader-memory-core` records maximum adverse and
favourable excursion per thesis for exactly this reason.

Without MAE, "our stops are too tight" (F-12) is an opinion. With it, it is a
measurement — and a config diff with evidence, which is the only kind of
doctrine change CLAUDE.md permits.

## F-18 (P2) — Quadrant attribution has no regime dimension

`classify_outcome()` (`scoring.py:36-48`) splits on P&L sign. A thesis that was
correct but lost money in a market-wide drawdown is filed as `unlucky_loss` if
accuracy held up, and `deserved_loss` if it didn't — with no way to see that
the market took it. `signal-postmortem` carries an explicit **`regime_mismatch`**
category alongside true/false positive.

Cheapest fix: classify on **market-adjusted** P&L once F-05 provides the
benchmark. Same 2×2, better denominator.

---

# 6. Considered and rejected

- **Investor-persona agents** (`ai-hedge-fund`, 62.6k★ — Buffett/Munger/Wood).
  Unfalsifiable by construction and incompatible with scoring our own
  calibration. Its own README says "not intended for real trading".
- **Autonomous execution / broker connectors** (`Vibe-Trading`, `TradingAgents`).
  Invariant 5. Their *guardrails* are worth copying; their execution path is not.
- **Chart-pattern recognition** (`PatternPy`, `TradingPatternScanner`).
  Re-admits discretionary pattern-matching through the back door.
- **TradingAgents' reported Sharpe ≥ 5.60** (arXiv 2412.20138) on AAPL/GOOGL/AMZN.
  Adopt the *architecture* ideas (memory/reflection storing alpha, role
  decomposition); do not treat that number as evidence of anything. A Sharpe
  above 5 on single-name equity is far outside what a real strategy sustains
  and points to a short window, look-ahead, or both. Hawkeye's own insistence
  on a measured forward ledger over borrowed backtests is the right instinct —
  which is the strongest argument for fixing F-05 and F-06 first.

---

# 7. Recommended order of work

Nothing here changes doctrine numbers without a commit-message rationale
(invariant 7), and nothing precedes the Phase 0 verdict except the items that
would otherwise make that verdict meaningless.

**Tier 1 — the Phase 0 experiment is invalid until these are done**
1. **F-01** RR/EV definitional fix — otherwise the BUY rate is an arithmetic artifact
2. **F-02** attack-id matching — otherwise BUYs die on paraphrase, silently
3. **F-04** Brier denominator — otherwise the headline metric is self-selected
4. **F-05** market-adjusted cohort returns — otherwise the verdict measures beta
5. **F-06** pre-registered statistical bar on the spread

**Tier 2 — highest value per unit of work, all cheap in session mode**
6. **F-07(b)** cross-model roles — a config change; research says ~60% error correlation today
7. **F-08** calibration feedback into the Bull + frequency-format elicitation
8. **F-10** `global-stock-data` into the dossier, estimate revisions first
9. **F-07(a)** Adversary's private disconfirmation packet (needs F-10)
10. **F-12** ATR-floored stops
11. **F-14** rank on EAR, not percentage surprise
12. **F-03** fail-closed hard gates

**Tier 3 — when the ledger shows the weakness**
13. F-13 portfolio heat → sector cap → circuit breaker
14. F-09 score the Judge's conviction (or stop claiming it is scored)
15. F-11 option-implied sanity check on scenarios
16. F-15 / F-16 / F-17 / F-18 dossier context, regime mode, MAE/MFE, regime-aware attribution

**The experiment worth running regardless (F-08):** thinking budget as a
pre-registered A/B against Brier. KalshiBench found extended reasoning made
calibration *worse* at comparable accuracy. CLAUDE.md currently specifies
adaptive thinking. Hawkeye has the rare machinery to settle this on its own
data — and settling it would be a genuine contribution, not just a tuning.

---

## Sources

Deliberation and calibration research: InfoDelphi / *Diverse Evidence, Better
Forecasts* (arXiv 2607.01661) · *The Wisdom of Deliberating AI Crowds*
(arXiv 2512.22625) · *The Deliberative Illusion* (arXiv 2606.03032) ·
KalshiBench (arXiv 2512.16030) · *Can LLM Agents Really Debate?*
(arXiv 2511.07784).

Repositories: `tradermonty/claude-trading-skills` (pre-trade-discipline-gate,
drawdown-circuit-breaker, position-sizer, trader-memory-core, signal-postmortem,
macro-regime-detector, earnings-trade-analyzer) · `TauricResearch/TradingAgents`
(memory/reflection, alpha_return) · `alex-jb/orallexa-ai-trading-agent`
(source-accuracy ledger, bias tracker, PASS/MARGINAL bar) ·
`simonlin1212/global-stock-data` (zero-auth US data) · `HKUDS/Vibe-Trading`
(mandate, kill switch, audit ledger) · `virattt/ai-hedge-fund` (rejected).

PEAD: Livnat & Mendenhall on revenue surprises; SUE vs EAR comparisons;
Quantpedia's post-earnings-announcement-effect summary.
