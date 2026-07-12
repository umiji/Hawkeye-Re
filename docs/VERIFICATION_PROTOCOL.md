# Verification Protocol

The adversarial process specification. This is the project's core IP: the
hypothesis is that this protocol, run mechanically, beats a human committee.

## Roles

| Role | Sees | Produces | Scored on |
|---|---|---|---|
| Entry Gates (code) | MarketSnapshot + Catalyst | GateReport | — (deterministic) |
| Bull (LLM) | dossier + gates | Thesis | Brier score of its claims |
| Adversary (LLM) | dossier + gates + thesis | AttackReport | kill-shots that later prove correct; penalized for severity inflation |
| Judge (LLM) | full written record | Verdict | calibration of `conviction` |
| Risk Officer (code) | thesis scenarios + prices | PositionPlan / veto | — (deterministic) |

Roles run as separate stateless LLM calls with different system prompts —
never one conversation — so no role can "feel" the others' preferences.

## The thesis contract (Bull)

A thesis is admissible only if it contains:

1. **Edge**: which mispricing mechanism, and why it exists *right now*.
2. **Other side**: who is selling at this price and why they are wrong.
   "Nobody has noticed" is inadmissible for a liquid stock (the sucker test).
3. **3–6 falsifiable claims** about the world (not "the stock will go up"),
   each with probability, horizon in days, and a verification method.
4. **Scenarios** (bear/base/bull) with probabilities and price targets
   consistent with base rates; probabilities renormalized by the pipeline.
5. **Kill criteria** including at least one price stop and one time stop.
   A thesis with no observable failure mode is not a thesis.

## The attack taxonomy (Adversary)

thesis_logic · valuation · catalyst_durability · crowding_positioning ·
liquidity · macro_regime · data_integrity · base_rate · timing ·
governance_accounting

Mandatory tests: the sucker test in reverse (argue the sellers are the
informed side), a probability audit of the Bull's most overconfident claim,
and a genuinely persuasive `strongest_short_case` (no strawmen).
Severity 5 = "if true, the trade is dead". Fewer, deadlier attacks score
better than volume; conceding a strong thesis scores better than noise.

## Judgment rules (pre-registered, enforced twice)

The Judge's prompt states the rules; `_judge_rule_check()` enforces them in
code, because prompts are instructions and code is law:

1. Default PASS. BUY requires an affirmative surviving case.
2. Every severity ≥ 4 attack must appear in `addressed` — refuted from the
   record, or converted into a kill criterion / monitored risk. An
   unaddressed severe attack mechanically overturns a BUY.
3. BUY with conviction < 0.55 is mechanically overturned.
4. The Risk Officer then applies reward/risk ≥ 2 and EV ≥ +5% hurdles and
   portfolio limits; any veto overturns the BUY with the reason appended to
   the rationale. The judge is explicitly told NOT to bend its judgment to
   the economics — the two checks are independent by design.

## Pre-registration and scoring loop

```
recommendation written to ledger (immutable, hash-chained)
        │
user Yes/No ─ trades recorded ─ sentinel signals recorded
        │
claims resolve TRUE/FALSE at horizon (journal events; payload never mutated)
        │
outcome = P&L × thesis accuracy ─► quadrant + Brier
        │
calibration table across the whole book ─► doctrine/prompt revisions (commits)
```

The quadrant logic:

|  | made money | lost money |
|---|---|---|
| **thesis right (accuracy ≥ 0.6)** | skill_win ✅ | unlucky_loss (acceptable) |
| **thesis wrong** | lucky_win ⚠️ process alarm | deserved_loss ⚠️ mandatory postmortem |

The system's success metric is the growth of the skill_win share and the
convergence of stated probabilities to observed frequencies — P&L follows.

## Known limitations (MVP honesty)

- One LLM vendor plays all three roles; true independence would use distinct
  models or ensembles per role (roadmap).
- The Adversary's reward ("kill-shots that prove correct") is not yet closed
  into an automated feedback loop — kill-shot resolution is manual for now.
- News input is headline-level; deep filing analysis is a roadmap item.
- Claim resolution is human-attested; automating verification per claim type
  is a roadmap item.
