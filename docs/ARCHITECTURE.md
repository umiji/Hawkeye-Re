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
| `hawkeye.gates` | Deterministic entry gates (pre-LLM) | part of tribunal svc |
| `hawkeye.tribunal` | Bull / Adversary / Judge LLM roles + orchestration | tribunal service |
| `hawkeye.risk` | Position sizing, portfolio limits, veto | risk service |
| `hawkeye.ledger` | Append-only journal, hash chain, Brier/attribution scoring | ledger service |
| `hawkeye.sentinel` | Daily kill-criteria checks on open positions | monitor service |
| `hawkeye.reports` | Japanese rendering of recommendations and signals | notification svc |
| `hawkeye.cli` | Manual operating surface for the MVP | scheduler/API gateway |

## Data flow

```
YahooProvider ─┐
               ├─ CompositeProvider ─ build_brief() ─► CandidateBrief
FinnhubProvider┘                                          │
                                                          ▼
                                            run_entry_gates()  ── hard fail ──► Recommendation(PASS)
                                                          │
                                                          ▼
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

## Integrity model

- Recommendation payloads are written once, never updated. Claim resolutions,
  decisions, trades, and outcomes are journal events referencing them.
- The journal is hash-chained (`hash = sha256(prev_hash‖ts‖rec‖kind‖payload)`);
  `hawkeye verify` detects any rewrite of history.
- The `status` column is a queryable projection; the journal is truth.

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
