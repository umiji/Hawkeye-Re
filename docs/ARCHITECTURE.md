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
| `hawkeye.marketdata` | Yahoo/Finnhub clients, indicators, CandidateBrief assembly | ingest service |
| `hawkeye.scout` | Mechanical candidate discovery (earnings-surprise screen, ranking) + cohort benchmark | scout service |
| `hawkeye.gates` | Deterministic entry gates (pre-LLM) | part of tribunal svc |
| `hawkeye.tribunal` | Bull / Adversary / Judge LLM roles + orchestration | tribunal service |
| `hawkeye.risk` | Position sizing, portfolio limits, veto | risk service |
| `hawkeye.ledger` | Append-only journal, hash chain, Brier/attribution scoring | ledger service |
| `hawkeye.sentinel` | Daily kill-criteria checks on open positions | monitor service |
| `hawkeye.reports` | Japanese rendering of recommendations and signals | notification svc |
| `hawkeye.cli` | Manual operating surface for the MVP | scheduler/API gateway |

## Data flow

```
Finnhub earnings calendar ─► scout: surprise screen ──┬─► survivors[:scout_max_enrich]
                                      │               └─► beyond the cap: DROPPED*
                                      │ (funnel counts recorded per scan)
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

`*` **DROPPED** = nothing survives per ticker today — only the aggregate funnel
counts in the `scans` table. This is a known gap, not a design choice: the
Phase 0 kill criterion ("BUYs must beat the reject pile", `docs/ROADMAP.md`) is
not measurable while most of the reject pile is unrecorded, and a missed winner
is invisible by construction. Note the asymmetry — a bad buy is bounded by the
stop; a missed winner is unbounded *and* silent. Recording every scanned
candidate, plus a market/beta baseline so cohort returns can be split into alpha
and beta, is a pending design: `docs/MASTER_OVERVIEW.ja.md` §5.1 (not yet
implemented). The comparison over what *is* recorded had its own bugs until
2026-07-28: manual `evaluate` picks leaked into the viability cohorts, and a
failed price-history fetch was silently dropped rather than counted as
censored — both fixed in `hawkeye/scout/benchmark.py`.

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
role. Separation is preserved mechanically — `casefile.write_package()` is
the single choke point deciding what each role may see (it reuses the same
renderers as API mode), and `case submit` re-validates every payload with
the same parsers before anything reaches the ledger. Records carry
`model="claude-code-session"` so the two engines' track records can be
compared cohort-style later.

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
