"""Case files — stepwise tribunal driving for external LLM engines.

"Session mode": instead of calling the Anthropic API (metered key), the
tribunal can be driven by a Claude Code session (subscription usage). The
orchestrating session spawns one fresh subagent per role and ferries JSON
in and out via the CLI:

    hawkeye case open TICKER ...    gates run; case created (or gate-PASS)
    hawkeye case step CASE_ID       emits ONLY the next role's allowed view
    hawkeye case submit CASE_ID -f  validates, stores, advances; on the last
                                    role: rule check -> risk officer ->
                                    ledger -> Japanese report

Information separation stays mechanical: `write_package()` is the single
place that decides what each role may see, mirroring the API driver's
renderers exactly. The orchestrator only moves files; it cannot leak the
attack report to the Bull because no Bull package ever contains one.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import (
    CandidateBrief,
    GateReport,
    Recommendation,
    new_id,
    utcnow,
)
from hawkeye.tribunal.pipeline import (
    assemble_recommendation,
    parse_attack_report,
    parse_thesis,
    parse_verdict,
)
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

SESSION_MODEL_LABEL = "claude-code-session"

ROLE_ORDER = ("bull", "adversary", "judge")


class Case(BaseModel):
    id: str = Field(default_factory=lambda: new_id("case"))
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())
    nav: float
    open_position_count: int = 0
    brief: CandidateBrief
    gate_report: GateReport
    thesis_raw: Optional[dict] = None
    attack_raw: Optional[dict] = None
    verdict_raw: Optional[dict] = None
    recommendation_id: Optional[str] = None   # set once finalized


def cases_dir() -> Path:
    return Path(os.environ.get("HAWKEYE_CASES", "cases"))


def _case_path(case_id: str) -> Path:
    return cases_dir() / f"{case_id}.json"


def save_case(case: Case) -> None:
    cases_dir().mkdir(parents=True, exist_ok=True)
    _case_path(case.id).write_text(case.model_dump_json(indent=2))


def load_case(case_id: str) -> Case:
    return Case.model_validate_json(_case_path(case_id).read_text())


def list_cases() -> list[Case]:
    if not cases_dir().exists():
        return []
    out = []
    for p in sorted(cases_dir().glob("case_*.json")):
        try:
            out.append(Case.model_validate_json(p.read_text()))
        except Exception:
            continue
    return out


def open_case(brief: CandidateBrief, gates: GateReport, nav: float,
              open_position_count: int = 0) -> Case:
    case = Case(nav=nav, open_position_count=open_position_count,
                brief=brief, gate_report=gates)
    save_case(case)
    return case


def next_role(case: Case) -> Optional[str]:
    if case.recommendation_id is not None:
        return None
    if case.thesis_raw is None:
        return "bull"
    if case.attack_raw is None:
        return "adversary"
    if case.verdict_raw is None:
        return "judge"
    return None


def write_package(case: Case) -> Optional[dict]:
    """Materialize the next role's system prompt, allowed input, and output
    schema as files. Returns paths + role, or None if the case is complete.

    This function is the information-separation boundary for session mode.
    """
    role = next_role(case)
    if role is None:
        return None
    if role == "bull":
        system, user, schema = (
            BULL_SYSTEM,
            render_bull_input(case.brief, case.gate_report),
            THESIS_SCHEMA)
    elif role == "adversary":
        system, user, schema = (
            ADVERSARY_SYSTEM,
            render_adversary_input(case.brief, case.gate_report,
                                   case.thesis_raw),
            ATTACK_SCHEMA)
    else:
        system, user, schema = (
            JUDGE_SYSTEM,
            render_judge_input(case.brief, case.gate_report,
                               case.thesis_raw, case.attack_raw),
            VERDICT_SCHEMA)

    role_dir = cases_dir() / case.id
    role_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "system": role_dir / f"{role}.system.md",
        "input": role_dir / f"{role}.input.json",
        "schema": role_dir / f"{role}.schema.json",
        "output": role_dir / f"{role}.out.json",   # where to write the reply
    }
    paths["system"].write_text(system)
    paths["input"].write_text(user)
    paths["schema"].write_text(json.dumps(schema, indent=2))
    return {"role": role, **{k: str(v) for k, v in paths.items()}}


def submit(case: Case, payload: dict) -> str:
    """Validate and store the next role's output. Returns the role consumed.
    Validation runs the same parsers as the API driver, so a malformed
    payload fails HERE, before anything reaches the ledger."""
    role = next_role(case)
    if role is None:
        raise ValueError("case already complete")
    if role == "bull":
        parse_thesis(payload)
        case.thesis_raw = payload
    elif role == "adversary":
        parse_attack_report(payload)
        case.attack_raw = payload
    else:
        parse_verdict(payload)
        case.verdict_raw = payload
    save_case(case)
    return role


def finalize(case: Case, config: HawkeyeConfig) -> Recommendation:
    """Deterministic tail (identical to API mode) once all roles submitted."""
    if next_role(case) is not None:
        raise ValueError(f"case not complete: next role is {next_role(case)}")
    rec = assemble_recommendation(
        case.brief, case.gate_report,
        case.thesis_raw, case.attack_raw, case.verdict_raw,
        config, nav=case.nav,
        open_position_count=case.open_position_count,
        model=SESSION_MODEL_LABEL)
    case.recommendation_id = rec.id
    save_case(case)
    return rec
