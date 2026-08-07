# Architecture

## Design constraints

1. **Loose coupling.** Each capability is a separate package that communicates
   only through `hawkeye.contracts` (pydantic models). Today they run in one
   process; any package can be lifted into a standalone microservice by
   putting its contract models on the wire — no other package changes.
2. **Determinism where possible, LLM only where necessary.** Gates, sizing,
   sentinel checks, scoring, and rule enforcement are plain code. LLMs are
   used exclusively for the three argumentative roles (Bull, Adversary,
   Judge), and even their outputs pass through deterministic rule checks.
3. **Pre-registration.** Nothing reaches the user that was not first written
   to an append-only, hash-chained ledger. The record is the product.

## Service map

| Package | Responsibility | Future service |
|---|---|---|
| `hawkeye.contracts` | Shared data models (the wire format) | schema registry |
| `hawkeye.marketdata` | Yahoo / Finnhub / EarningsWhispers / EDGAR clients, indicators, CandidateBrief assembly | ingest service |
| `hawkeye.scout` | Mechanical candidate discovery: surprise screen, vendor choice (`numbers.py`), hold-for-actuals (`waiting.py`), structural triage, ranking + drop review | scout service |
| `hawkeye.gates` | Deterministic entry gates (pre-LLM) | part of tribunal svc |
| `hawkeye.tribunal` | Bull / Adversary / Judge LLM roles + orchestration | tribunal service |
| `hawkeye.risk` | Position sizing, portfolio limits, veto | risk service |
| `hawkeye.ledger` | Append-only journal, hash chain, Brier/attribution scoring | ledger service |
| `hawkeye.sentinel` | Daily kill-criteria checks on open positions | monitor service |
| `hawkeye.reports` | Japanese rendering of recommendations and signals | notification svc |
| `hawkeye.cli` | Manual operating surface for the MVP | scheduler/API gateway |

## Data flow

```
Finnhub earnings calendar ─► screen #1 (provisional, cheap)
       │
       ├─► recorded by an earlier scan, or structurally refused before (§6.1(E)) ─► skipped
       ▼
EarningsWhispers ─► ONE vendor supplies this print's actual AND the consensus
 (scout_max_whispers)   it is measured against; if it cannot, the WHOLE print
       │                falls back to the calendar's figures
       ▼
screen #2 (the ranking that counts)
       │
       ├─► the print's own numbers have not arrived ─► HELD*
       ▼
walk the ranking one name at a time until scout_target_gate_passed have PASSED
the gates (or scout_max_enrich attempts are spent)  ──► the rest: DROPPED*
       │
       ▼
YahooProvider ─┐
               ├─ CompositeProvider ─ build_brief() ─► CandidateBrief
FinnhubProvider┘   (scout candidates and manual entries both land here)
                                                          │
                                                          ▼
                                            run_entry_gates()
                                             │        └── hard fail ──┬─ manual `evaluate`:
                                             │                        │    gate_only_recommendation()
                                             │                        │    ─► Recommendation(PASS)
                                             │                        └─ scout path: DROPPED*
                                             ▼
                                            ranked shortlist ──┬─► top N ─► tribunal
                                                               └─► below the cutoff: DROPPED*
                     Bull ──Thesis──► Adversary ──AttackReport──► Judge ──Verdict
                      (sees brief+gates)   (sees +thesis)          (sees whole record)
                                                          │
                                                          ▼
                                            _judge_rule_check()   # code, not prompt
                                            build_position_plan() # risk officer veto
                                                          │
                                                          ▼
                                            Recommendation ─► Ledger (append-only)
                                                          │
                                                          ▼
                                            render_recommendation_ja() ─► user
```

`*` Every one of those exits is recorded per ticker in `screened_candidates`,
with the stage that produced it (`actual_pending` / `actual_timeout` /
`enrichment_cap` / `gate_reject` / `ranking_cutoff`), the numbers the screen
saw, and the qualitative data that was visible at drop time. That is what makes
the Phase 0 kill criterion ("BUYs must beat the reject pile",
`strategy/ROADMAP.md`) measurable at all. The asymmetry is why it matters: a
bad buy is bounded by the stop, while a missed winner is unbounded *and*
silent, so the reject pile has to be as legible as the buys.
`hawkeye/scout/drop_review.py` splits cohort returns against a market baseline.
The comparison had its own bugs until 2026-07-28: manual `evaluate` picks
leaked into the viability cohorts, and a failed price-history fetch was
silently dropped rather than counted as censored — both fixed in
`hawkeye/scout/benchmark.py`.

⚠️ **`actual_pending` is the one stage the dedup ignores.** Those prints were
never judged — the feed had not published their numbers yet — so counting them
as seen would record a print as pending exactly once and never read it again
(`hawkeye/scout/waiting.py`).

Post-trade lifecycle (all journal events referencing the recommendation):

```
user_decision ─► entry_trade ─► sentinel_signal* ─► claim_resolution* ─► exit_trade ─► outcome
```

## Information separation (why three LLM calls, not one)

Each role gets a different, minimal view of the record:

- **Bull** sees the dossier and gate results — never the attacks.
- **Adversary** sees the dossier and the *written* thesis — never the Bull's
  private reasoning, and it is a fresh context, not a continued conversation.
- **Judge** sees the full written record and may not introduce new facts.

A single conversation would let agreement leak across roles (the model
softening its own attack because it "knows" it wants to buy). Separate
stateless calls make the debate structural rather than performative.

The Judge's obligation to address every severity>=4 attack is mechanically
checked (`_judge_rule_check`), not just requested in the prompt — matching
is by `Attack.id` (content-hashed, assigned by `parse_attack_report`), not
text similarity. Before 2026-07-28 this matched on a 60-character substring
of the attack's wording, which a paraphrased judge response would fail even
when it genuinely addressed the attack, silently overturning correct BUYs.

## Integrity model

- Recommendation payloads are written once, never updated. Claim resolutions,
  decisions, trades, and outcomes are journal events referencing them.
- The journal is hash-chained (`hash = sha256(prev_hash‖ts‖rec‖kind‖payload)`);
  `hawkeye verify` detects any rewrite of history.
- The `status` column is a queryable projection; the journal is truth.

## Two LLM drivers, one deterministic tail

```
API mode:      run_tribunal()  ── AnthropicLLM (metered key) ──┐
                                                               ├─ assemble_recommendation()
Session mode:  casefile (case open/step/submit CLI)            │   parsers → judge-rule check
               driven by /hawkeye-run in Claude Code ──────────┘   → risk veto → ledger
```

Session mode exists so the system runs on a Claude subscription with no API
key: the Claude Code session orchestrates, spawning one fresh subagent per
role. Separation is preserved mechanically for the *content* each role's
subagent receives — `casefile.write_package()` is the single choke point
deciding what each role may see (it reuses the same renderers as API mode),
and `case submit` re-validates every payload with the same parsers before
anything reaches the ledger. Records carry `model="claude-code-session"` so
the two engines' track records can be compared cohort-style later.

**Known limitation (accepted 2026-07-28, not fixed):** unlike API mode's
three genuinely separate stateless calls, the session-mode orchestrator (the
top-level Claude Code session) has raw filesystem read access to every
role's file in the case directory — nothing in code stops it from reading
`adversary.out.json` before spawning the Bull subagent. The boundary that
matters at that layer is operational discipline documented in
`.claude/skills/hawkeye-run/SKILL.md` ("never author or edit role JSON"),
not a code-enforced sandbox. True technical isolation isn't achievable
within this architecture (a subagent is always spawned by, and inherits the
trust of, its parent session), so this is disclosed rather than "fixed".
Use API mode when strict technical separation matters.

## LLM usage

- Model: `claude-opus-4-8` (override with `HAWKEYE_MODEL`), adaptive thinking,
  structured outputs (`output_config.format` with hand-written JSON schemas —
  no numeric constraints; bounds are clamped by pipeline parsers instead).
- All three prompts state the role's *scoring rule*, not just its task:
  the Bull is Brier-scored, the Adversary is penalized for severity inflation,
  the Judge's conviction is treated as a calibrated probability. Incentive
  framing is the cheapest defense against sycophancy.
- `ScriptedLLM` replays canned responses so the entire pipeline is testable
  offline; `AnthropicLLM` is the production client.
