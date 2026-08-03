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
import shutil
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from hawkeye import paths
from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import (
    CandidateBrief,
    GateReport,
    Recommendation,
    new_id,
    now,
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
    created_at: str = Field(default_factory=lambda: now().isoformat())
    nav: float
    open_position_count: int = 0
    brief: CandidateBrief
    gate_report: GateReport
    thesis_raw: Optional[dict] = None
    attack_raw: Optional[dict] = None
    verdict_raw: Optional[dict] = None
    recommendation_id: Optional[str] = None   # set once finalized


def cases_dir() -> Path:
    return paths.cases_dir()


def _case_path(case_id: str) -> Path:
    return cases_dir() / f"{case_id}.json"


def save_case(case: Case) -> None:
    cases_dir().mkdir(parents=True, exist_ok=True)
    _case_path(case.id).write_text(case.model_dump_json(indent=2), encoding="utf-8")


def load_case(case_id: str) -> Case:
    return Case.model_validate_json(_case_path(case_id).read_text(encoding="utf-8"))


def list_cases() -> list[Case]:
    if not cases_dir().exists():
        return []
    out = []
    for p in sorted(cases_dir().glob("case_*.json")):
        try:
            out.append(Case.model_validate_json(p.read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"warning: skipping unreadable case file {p}: {exc}",
                  file=sys.stderr)
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
        # Parse once so the Adversary argues over the same normalized
        # numbers (clamped probabilities, renormalized scenario weights)
        # that end up in the stored record, not the Bull's raw output.
        # This and every later re-parse of case.thesis_raw agree ONLY
        # because claim ids are content-derived (`claim_content_id`). While
        # they came from a random uuid factory the three parses below —
        # here, the Judge's package, and finalize() — each minted a
        # different set, so the Adversary cited claim ids the Judge could
        # not find (2026-08-01 fix). Keep ids a pure function of content.
        thesis_for_render = parse_thesis(case.thesis_raw).model_dump(
            mode="json")
        system, user, schema = (
            ADVERSARY_SYSTEM,
            render_adversary_input(case.brief, case.gate_report,
                                   thesis_for_render),
            ATTACK_SCHEMA)
    else:
        thesis_for_render = parse_thesis(case.thesis_raw).model_dump(
            mode="json")
        # Parse once so the Judge sees the same attack ids finalize() will
        # later match `addressed[].attack_id` against (parse_attack_report
        # is deterministic — re-parsing case.attack_raw agrees on the ids).
        attacks_for_judge = parse_attack_report(case.attack_raw).model_dump(
            mode="json")
        system, user, schema = (
            JUDGE_SYSTEM,
            render_judge_input(case.brief, case.gate_report,
                               thesis_for_render, attacks_for_judge),
            VERDICT_SCHEMA)

    role_dir = cases_dir() / case.id
    role_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "system": role_dir / f"{role}.system.md",
        "input": role_dir / f"{role}.input.json",
        "schema": role_dir / f"{role}.schema.json",
        "output": role_dir / f"{role}.out.json",   # where to write the reply
    }
    paths["system"].write_text(system, encoding="utf-8")
    paths["input"].write_text(user, encoding="utf-8")
    paths["schema"].write_text(json.dumps(schema, indent=2), encoding="utf-8")
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
    """Deterministic tail (identical to API mode) once all roles submitted.

    Does NOT mark the case complete — call mark_complete() only once the
    caller has durably recorded the returned Recommendation (e.g. the
    ledger insert succeeded). Marking complete first would let a ledger
    write failure leave the case looking done with no matching ledger row;
    next_role() already reports "complete" once all three role JSONs are
    present (it doesn't depend on recommendation_id), so nothing here gets
    stuck asking for a fourth role while recording is retried.
    """
    if next_role(case) is not None:
        raise ValueError(f"case not complete: next role is {next_role(case)}")
    return assemble_recommendation(
        case.brief, case.gate_report,
        case.thesis_raw, case.attack_raw, case.verdict_raw,
        config, nav=case.nav,
        open_position_count=case.open_position_count,
        model=SESSION_MODEL_LABEL)


def mark_complete(case: Case, recommendation_id: str) -> None:
    """Record that `case` produced `recommendation_id`. Call only after the
    caller has confirmed it's durably stored (e.g. ledger insert succeeded)."""
    case.recommendation_id = recommendation_id
    save_case(case)
    # Only now — the role workspace is what makes a failed ledger write
    # retryable, and `submit()` refuses to re-answer a completed role
    # (docs/design/MASTER_OVERVIEW.ja.md §5.2(7); ordering per the M5 fix).
    _remove_role_workspace(case.id)


def _remove_role_workspace(case_id: str) -> bool:
    """Delete one case's per-role scratch folder. Returns whether anything
    was there. Every file in it is either regenerated deterministically by
    `write_package()` or already copied verbatim into the case JSON by
    `submit()`, so there is no version worth keeping and no need for an
    opt-out flag."""
    workspace = cases_dir() / case_id
    if not workspace.is_dir():
        return False
    shutil.rmtree(workspace)
    return True


def sweep_role_workspaces() -> list[str]:
    """Remove leftover workspaces of cases whose recommendation is already in
    the ledger; returns the case ids cleaned up.

    Runs automatically rather than as a command to remember (§5.2(8)). Cases
    still in progress are untouched — an unfinished case's workspace is its
    resume point, not garbage.
    """
    removed: list[str] = []
    for case in list_cases():
        if case.recommendation_id and _remove_role_workspace(case.id):
            removed.append(case.id)
    return removed
