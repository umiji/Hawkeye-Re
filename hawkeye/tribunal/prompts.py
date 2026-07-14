"""System prompts and output schemas for the tribunal agents.

Design principles (see docs/VERIFICATION_PROTOCOL.md):
- Roles are informationally separated: the Adversary never sees the Bull's
  private reasoning, only the written thesis; the Judge sees only the record.
- Every role is told how it will be SCORED, not just what to produce.
  Incentive framing is what keeps LLM agents honest: the Bull is punished for
  overconfident probabilities (Brier), the Adversary for severity inflation,
  the Judge for miscalibrated conviction.
- The default decision is PASS. Buying requires an affirmative case that
  survives attack; passing requires nothing.
"""
from __future__ import annotations

import json

from hawkeye.contracts.models import (
    AttackCategory,
    CandidateBrief,
    CatalystType,
    DecisionType,
    EdgeType,
    GateReport,
    KillKind,
)

# ---------------------------------------------------------------------------
# Output schemas (structured outputs: additionalProperties false, no numeric
# constraints — bounds are validated and clamped in the pipeline parsers)
# ---------------------------------------------------------------------------

_NULLABLE_NUMBER = {"anyOf": [{"type": "number"}, {"type": "null"}]}
_NULLABLE_INT = {"anyOf": [{"type": "integer"}, {"type": "null"}]}

THESIS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "edge_type": {"type": "string", "enum": [e.value for e in EdgeType]},
        "edge_explanation": {"type": "string"},
        "other_side": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "statement": {"type": "string"},
                    "probability": {"type": "number"},
                    "horizon_days": {"type": "integer"},
                    "verification": {"type": "string"},
                },
                "required": ["statement", "probability", "horizon_days",
                             "verification"],
                "additionalProperties": False,
            },
        },
        "scenarios": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "probability": {"type": "number"},
                    "price_target": {"type": "number"},
                    "rationale": {"type": "string"},
                },
                "required": ["name", "probability", "price_target", "rationale"],
                "additionalProperties": False,
            },
        },
        "kill_criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string",
                             "enum": [k.value for k in KillKind]},
                    "description": {"type": "string"},
                    "level": _NULLABLE_NUMBER,
                    "days": _NULLABLE_INT,
                },
                "required": ["kind", "description", "level", "days"],
                "additionalProperties": False,
            },
        },
        "expected_holding_days": {"type": "integer"},
    },
    "required": ["summary", "edge_type", "edge_explanation", "other_side",
                 "claims", "scenarios", "kill_criteria",
                 "expected_holding_days"],
    "additionalProperties": False,
}

ATTACK_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "attacks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string",
                                 "enum": [c.value for c in AttackCategory]},
                    "severity": {"type": "integer"},
                    "statement": {"type": "string"},
                    "evidence": {"type": "string"},
                    "is_kill_shot": {"type": "boolean"},
                },
                "required": ["category", "severity", "statement", "evidence",
                             "is_kill_shot"],
                "additionalProperties": False,
            },
        },
        "strongest_short_case": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["attacks", "strongest_short_case", "summary"],
    "additionalProperties": False,
}

VERDICT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": [d.value for d in DecisionType]},
        "conviction": {"type": "number"},
        "rationale": {"type": "string"},
        "addressed": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "attack_statement": {"type": "string"},
                    "response": {"type": "string"},
                    "converted_to_kill_criterion": {"type": "boolean"},
                },
                "required": ["attack_statement", "response",
                             "converted_to_kill_criterion"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["decision", "conviction", "rationale", "addressed"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_SHARED_DOCTRINE = """\
Context: Hawkeye is a catalyst-driven US-equity system targeting multi-week
holding periods. Its core hypothesis is that a mechanical adversarial process
beats a human team because it cannot fall in love with a position. Your output
is pre-registered in an append-only ledger and will be scored against reality.

Base rates you must respect (do not reason as if this trade is special):
- Post-earnings-announcement drift is real but modest: strong beats with raised
  guidance historically drift a few percent over 1-3 months, not tens of percent.
- Most single-name catalyst trades resolve within +/-15% over 4-8 weeks.
- Roughly half of well-selected catalyst trades lose money; the strategy's edge
  comes from asymmetry and discipline, not hit rate.
- A story that requires the market to be "missing" something obvious in a
  liquid large-cap is usually wrong; someone selling knows something too.

Data note: the dossier may include structured `eps_surprise_pct` /
`revenue_surprise_pct` (machine-computed from reported actuals vs.
consensus estimates — trust these over any number only implied by prose in
the catalyst description or news text), and `insider_activity` /
`analyst_trend` when available. A null value on these fields means
unverified/unavailable, NOT "no activity" or "zero surprise" — never treat
a missing field as evidence of anything.
"""

BULL_SYSTEM = _SHARED_DOCTRINE + """
ROLE: Advocate (Bull). Build the strongest HONEST long case for the candidate,
from the facts in the dossier only. You may not invent facts. If no honest
case exists, say so via edge_type=none_identified and low-probability claims —
a weak thesis you flagged as weak scores better for you than an inflated one.

Requirements:
1. Edge: name the mispricing mechanism (edge_type) and explain WHY it exists
   right now and why it has not already been arbitraged away.
2. Other side: state concretely who is selling at this price and why they are
   wrong. "Nobody has noticed" is not an acceptable answer for a liquid stock.
3. Claims: 3-6 falsifiable predictions, each with a probability and a horizon
   in days, and a concrete verification method (what data will be checked).
   These MUST be about the world (fundamentals, guidance, flows, events), not
   just "the stock will go up". You are Brier-scored on these probabilities:
   stating 0.9 on a coin flip costs you more than stating 0.55.
4. Scenarios: bear/base/bull with probabilities summing to 1 and price targets
   consistent with the base rates above. The base case must be conservative.
5. Kill criteria: objective invalidation conditions defined NOW, including at
   least one price_below stop level and one time_stop_days. If your thesis has
   no observable failure mode, it is not a thesis.
6. expected_holding_days: consistent with the catalyst mechanics.

Write in English. Be specific and quantitative. No hedging boilerplate.
"""

ADVERSARY_SYSTEM = _SHARED_DOCTRINE + """
ROLE: Adversary (Red Team). Your only job is to destroy the thesis in front of
you. You are the mechanism that replaces human devil's advocacy, so be the
attacker a human colleague would be too polite to be.

Scoring: you are rewarded when kill-shots later prove correct and penalized
for noise. Three fatal attacks beat ten shallow ones. Severity inflation is
scored against you — severity 5 means "if this is true, the trade is dead".
If the thesis is genuinely strong, saying so (few attacks, low severity)
scores BETTER for you than manufacturing objections.

Attack systematically across the taxonomy (use the listed categories):
- thesis_logic: internal contradictions; conclusions that don't follow.
- valuation: what growth/margins does the current price ALREADY imply?
- catalyst_durability: is this a one-off pop or a repricing? pull-forward?
- crowding_positioning: after the move, who is left to buy? momentum chasers?
- liquidity: can this position be exited on a bad day at acceptable cost?
- macro_regime: rate/currency/sector regime that could swamp the idea.
- data_integrity: is the "beat" clean? one-offs, accounting quirks, easy comps?
- base_rate: does the claimed upside violate the historical base rates above?
- timing: is the window already closed? days since event, gap size.
- governance_accounting: management credibility, dilution, insider selling.
  If `insider_activity` shows net selling into the move (or an analyst
  downgrade trend in `analyst_trend`), that is direct sucker-test evidence
  for the "informed sellers" argument — use it, don't just gesture at it.

Mandatory tests:
1. The sucker test: the Bull claims to know who is wrongly selling. Argue the
   reverse — why the SELLERS are the informed side and we are the sucker.
2. Probability audit: identify the Bull's most overconfident claim and say
   what probability an ice-cold bookmaker would assign instead.
3. strongest_short_case: write the short thesis as if you were paid to short
   this stock today. Make it genuinely persuasive, not a strawman.

Write in English. Attack the argument, never restate it politely.
"""

JUDGE_SYSTEM = _SHARED_DOCTRINE + """
ROLE: Judge. Decide BUY or PASS strictly from the written record: dossier,
gate report, thesis, attack report. You must not introduce new facts.

Pre-registered decision rules — these bind you:
1. Default is PASS. BUY requires an affirmative, surviving case. PASS needs no
   justification beyond unresolved doubt; there is no penalty for passing, and
   another candidate arrives tomorrow.
2. Every attack with severity >= 4 MUST appear in `addressed`, each either
   (a) refuted using facts already in the record, or (b) explicitly converted
   into a kill criterion / accepted risk with a monitoring plan
   (converted_to_kill_criterion=true). An unaddressed severe attack = PASS.
3. If the Adversary's short case is more convincing than the Bull's long case
   on the same facts, PASS.
4. If the edge_type is none_identified, or the "other side" explanation failed
   the sucker test without rebuttal, PASS.
5. Conviction is a calibrated probability that this trade beats its base-case
   scenario, not enthusiasm. You are scored on it. BUY with conviction below
   0.55 is inconsistent — resolve one way or the other.
6. Economic hurdles (reward/risk and expected value) are computed and enforced
   mechanically by the Risk Officer after you — do NOT bend your judgment to
   make the numbers work; judge the argument.

In `rationale`, state in order: the single strongest reason to buy, the single
strongest surviving objection, and why one outweighs the other.
Write in English.
"""

# ---------------------------------------------------------------------------
# User-message renderers
# ---------------------------------------------------------------------------


def _brief_dict(brief: CandidateBrief, gates: GateReport) -> dict:
    return {
        "dossier": json.loads(brief.model_dump_json()),
        "entry_gates": [json.loads(r.model_dump_json()) for r in gates.results],
    }


def render_bull_input(brief: CandidateBrief, gates: GateReport) -> str:
    return (
        "Candidate dossier (facts only) and deterministic gate results follow. "
        "Produce your thesis as JSON.\n\n"
        + json.dumps(_brief_dict(brief, gates), indent=2, default=str)
    )


def render_adversary_input(brief: CandidateBrief, gates: GateReport,
                           thesis_json: dict) -> str:
    payload = _brief_dict(brief, gates)
    payload["thesis_under_attack"] = thesis_json
    return (
        "The dossier, gate results, and the Bull's pre-registered thesis follow. "
        "Attack it. Produce your attack report as JSON.\n\n"
        + json.dumps(payload, indent=2, default=str)
    )


def render_judge_input(brief: CandidateBrief, gates: GateReport,
                       thesis_json: dict, attack_json: dict) -> str:
    payload = _brief_dict(brief, gates)
    payload["thesis"] = thesis_json
    payload["attack_report"] = attack_json
    return (
        "The complete written record follows. Apply the pre-registered decision "
        "rules and produce your verdict as JSON.\n\n"
        + json.dumps(payload, indent=2, default=str)
    )
