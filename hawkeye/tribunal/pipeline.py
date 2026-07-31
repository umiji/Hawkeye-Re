"""Tribunal orchestration.

Flow (docs/VERIFICATION_PROTOCOL.md):

    gates -> [hard fail => SYSTEM PASS, zero LLM spend]
          -> Bull (thesis)      : sees dossier + gates only
          -> Adversary (attack) : sees dossier + gates + thesis
          -> Judge (verdict)    : sees the whole written record
          -> Risk Officer       : deterministic sizing; can veto a BUY

The pipeline owns all normalization of LLM output (probability clamping,
scenario renormalization, enum fallbacks) so a slightly-off model reply never
corrupts the pre-registered record.
"""
from __future__ import annotations


from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import (
    AddressedAttack,
    Attack,
    attack_content_id,
    AttackCategory,
    AttackReport,
    CandidateBrief,
    Claim,
    DecisionType,
    EdgeType,
    GateReport,
    KillCriterion,
    KillKind,
    Recommendation,
    Scenario,
    Thesis,
    Verdict,
)
from hawkeye.gates.entry_gates import run_entry_gates
from hawkeye.risk.sizing import build_position_plan, expected_value_pct
from hawkeye.tribunal.llm import LLMClient
from hawkeye.tribunal.prompts import (
    ADVERSARY_SYSTEM,
    ATTACK_SCHEMA,
    BULL_SYSTEM,
    JUDGE_SYSTEM,
    THESIS_SCHEMA,
    VERDICT_SCHEMA,
    render_adversary_input,
    render_bull_input,
    render_judge_input,
)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _enum_or(enum_cls, value, fallback):
    try:
        return enum_cls(value)
    except ValueError:
        return fallback


# Attack ids are content-derived so the two parses of one raw dict per run
# — once to bake ids into the view rendered to the Judge, once inside
# assemble_recommendation() to build the final record — always agree. The
# definition lives in contracts (`attack_content_id`) because loading an old
# ledger row has to recompute the very same id, and contracts must not
# import the pipeline.


def parse_thesis(raw: dict) -> Thesis:
    scenarios = [
        Scenario(name=s["name"], probability=_clamp01(s["probability"]),
                 price_target=float(s["price_target"]),
                 rationale=s.get("rationale", ""))
        for s in raw.get("scenarios", [])
    ]
    total = sum(s.probability for s in scenarios)
    if total > 0:
        scenarios = [s.model_copy(update={"probability": s.probability / total})
                     for s in scenarios]
    return Thesis(
        summary=raw["summary"],
        edge_type=_enum_or(EdgeType, raw.get("edge_type"),
                           EdgeType.NONE_IDENTIFIED),
        edge_explanation=raw.get("edge_explanation", ""),
        other_side=raw.get("other_side", ""),
        claims=[
            Claim(statement=c["statement"],
                  probability=_clamp01(c["probability"]),
                  horizon_days=max(1, int(c["horizon_days"])),
                  verification=c.get("verification", ""))
            for c in raw.get("claims", [])
        ],
        scenarios=scenarios,
        kill_criteria=[
            KillCriterion(kind=_enum_or(KillKind, k.get("kind"), KillKind.EVENT),
                          description=k.get("description", ""),
                          level=k.get("level"),
                          days=k.get("days"))
            for k in raw.get("kill_criteria", [])
        ],
        expected_holding_days=max(1, int(raw.get("expected_holding_days", 30))),
    )


def parse_attack_report(raw: dict) -> AttackReport:
    return AttackReport(
        attacks=[
            Attack(id=a.get("id") or attack_content_id(
                       a.get("category", ""), a["statement"],
                       a.get("evidence", "")),
                   category=_enum_or(AttackCategory, a.get("category"),
                                     AttackCategory.THESIS_LOGIC),
                   severity=max(1, min(5, int(a["severity"]))),
                   statement=a["statement"],
                   evidence=a.get("evidence", ""),
                   is_kill_shot=bool(a.get("is_kill_shot", False)))
            for a in raw.get("attacks", [])
        ],
        strongest_short_case=raw.get("strongest_short_case", ""),
        summary=raw.get("summary", ""),
    )


def parse_verdict(raw: dict) -> Verdict:
    return Verdict(
        decision=_enum_or(DecisionType, raw.get("decision"), DecisionType.PASS),
        conviction=_clamp01(raw.get("conviction", 0.0)),
        rationale=raw.get("rationale", ""),
        addressed=[
            AddressedAttack(
                attack_id=a.get("attack_id", ""),
                attack_statement=a.get("attack_statement", ""),
                response=a.get("response", ""),
                converted_to_kill_criterion=bool(
                    a.get("converted_to_kill_criterion", False)))
            for a in raw.get("addressed", [])
        ],
    )


def _judge_rule_check(verdict: Verdict, attacks: AttackReport) -> list[str]:
    """Mechanical enforcement of the judge's pre-registered rules.

    The judge is told the rules in its prompt, but prompts are not
    guarantees — this code is. Violations flip a BUY to PASS.
    """
    violations: list[str] = []
    if verdict.decision != DecisionType.BUY:
        return violations
    addressed_ids = {a.attack_id for a in verdict.addressed if a.attack_id}
    for attack in attacks.severe:
        if attack.id not in addressed_ids:
            violations.append(
                f"severity-{attack.severity} attack not addressed: "
                f"{attack.statement[:120]}")
    if verdict.conviction < 0.55:
        violations.append(
            f"BUY with conviction {verdict.conviction:.2f} < 0.55 is inconsistent")
    return violations


def _stop_and_target(thesis: Thesis, entry_price: float) -> tuple[float, float]:
    stops = [k.level for k in thesis.kill_criteria
             if k.kind == KillKind.PRICE_BELOW and k.level is not None]
    stop = max(stops) if stops else entry_price * 0.92
    base = next((s for s in thesis.scenarios if s.name.lower() == "base"), None)
    if base is None and thesis.scenarios:
        base = sorted(thesis.scenarios, key=lambda s: s.probability)[-1]
    target = base.price_target if base is not None else entry_price * 1.10
    return stop, target


def gate_only_recommendation(brief: CandidateBrief,
                             gates: GateReport) -> Recommendation:
    reasons = "; ".join(
        f"{g.name} (value={g.value}, threshold={g.threshold}"
        + (f", {g.note}" if g.note else "") + ")"
        for g in gates.hard_failures)
    return Recommendation(
        ticker=brief.ticker, brief=brief, gate_report=gates,
        verdict=Verdict(decision=DecisionType.PASS, conviction=0.0,
                        rationale=f"Hard entry-gate failure: {reasons}"),
        model="(gates only)")


def assemble_recommendation(
    brief: CandidateBrief,
    gates: GateReport,
    thesis_raw: dict,
    attack_raw: dict,
    verdict_raw: dict,
    config: HawkeyeConfig,
    nav: float,
    open_position_count: int,
    model: str,
) -> Recommendation:
    """Deterministic tail of the tribunal: parsing, rule enforcement, risk
    officer, final record. Shared by the API driver (run_tribunal) and the
    session driver (casefile) so both modes produce identical records."""
    thesis = parse_thesis(thesis_raw)
    attacks = parse_attack_report(attack_raw)
    verdict = parse_verdict(verdict_raw)

    entry_price = brief.snapshot.price
    verdict.expected_value_pct = round(
        expected_value_pct(thesis.scenarios, entry_price), 2)

    violations = _judge_rule_check(verdict, attacks)
    if violations:
        verdict.decision = DecisionType.PASS
        verdict.rationale += (
            "\n[RULE ENFORCEMENT] BUY overturned to PASS: " + "; ".join(violations))

    plan = None
    if verdict.decision == DecisionType.BUY:
        stop, target = _stop_and_target(thesis, entry_price)
        plan = build_position_plan(
            nav=nav, entry_price=entry_price, stop_price=stop,
            target_price=target, scenarios=thesis.scenarios, config=config,
            open_position_count=open_position_count)
        plan = plan.model_copy(update={
            "max_holding_days": min(config.max_holding_days,
                                    thesis.expected_holding_days * 2)})
        verdict.reward_risk = plan.reward_risk
        if not plan.approved:
            verdict.decision = DecisionType.PASS
            verdict.rationale += (
                "\n[RISK OFFICER VETO] " + "; ".join(plan.vetoes))

    return Recommendation(
        ticker=brief.ticker, brief=brief, gate_report=gates, thesis=thesis,
        attack_report=attacks, verdict=verdict, plan=plan, model=model)


def run_tribunal(
    brief: CandidateBrief,
    llm: LLMClient,
    config: HawkeyeConfig,
    nav: float = 100_000.0,
    open_position_count: int = 0,
) -> Recommendation:
    gates: GateReport = run_entry_gates(brief.snapshot, brief.catalyst, config)
    if not gates.ok:
        return gate_only_recommendation(brief, gates)

    thesis_raw = llm.complete_json(
        BULL_SYSTEM, render_bull_input(brief, gates), THESIS_SCHEMA)
    # Parse once so the Adversary/Judge argue over the same normalized
    # numbers (clamped probabilities, renormalized scenario weights) that
    # end up in the stored record — parse_thesis is deterministic, so this
    # and assemble_recommendation's later re-parse of thesis_raw agree.
    thesis_for_render = parse_thesis(thesis_raw).model_dump(mode="json")
    attack_raw = llm.complete_json(
        ADVERSARY_SYSTEM,
        render_adversary_input(brief, gates, thesis_for_render),
        ATTACK_SCHEMA)
    # Parse once so the Judge sees the same attack ids assemble_recommendation
    # will later match `addressed[].attack_id` against (parse_attack_report
    # is deterministic — a second parse of attack_raw agrees on the same ids).
    attacks_for_judge = parse_attack_report(attack_raw).model_dump(mode="json")
    verdict_raw = llm.complete_json(
        JUDGE_SYSTEM,
        render_judge_input(brief, gates, thesis_for_render, attacks_for_judge),
        VERDICT_SCHEMA)

    return assemble_recommendation(
        brief, gates, thesis_raw, attack_raw, verdict_raw, config,
        nav=nav, open_position_count=open_position_count,
        model=getattr(llm, "model", type(llm).__name__))
