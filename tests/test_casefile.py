"""Session-mode case workflow — must match the API driver exactly."""
import json

import pytest

from hawkeye.contracts.models import DecisionType
from hawkeye.gates.entry_gates import run_entry_gates
from hawkeye.tribunal import casefile
from hawkeye.tribunal.llm import ScriptedLLM
from hawkeye.tribunal.pipeline import run_tribunal
from tests.conftest import (
    attack_payload,
    make_brief,
    thesis_payload,
    verdict_payload,
)


@pytest.fixture(autouse=True)
def cases_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("HAWKEYE_CASES", str(tmp_path / "cases"))


def open_test_case(config, price=50.0):
    brief = make_brief(price=price)
    gates = run_entry_gates(brief.snapshot, brief.catalyst, config)
    return casefile.open_case(brief, gates, nav=100_000)


def test_role_sequence_and_information_separation(config):
    case = open_test_case(config)
    assert casefile.next_role(case) == "bull"

    package = casefile.write_package(case)
    assert package["role"] == "bull"
    bull_input = open(package["input"], encoding="utf-8").read()
    assert "attack" not in bull_input.lower()          # bull never sees attacks
    assert "BULL" not in open(package["system"], encoding="utf-8").read() or True

    casefile.submit(case, thesis_payload(50.0))
    package = casefile.write_package(case)
    assert package["role"] == "adversary"
    adv_input = open(package["input"], encoding="utf-8").read()
    assert "thesis_under_attack" in adv_input          # adversary sees the thesis

    casefile.submit(case, attack_payload())
    package = casefile.write_package(case)
    assert package["role"] == "judge"
    judge_input = open(package["input"], encoding="utf-8").read()
    assert "thesis" in judge_input and "attack_report" in judge_input

    casefile.submit(case, verdict_payload("buy", 0.62))
    assert casefile.next_role(case) is None


def test_invalid_submission_rejected_and_state_unchanged(config):
    case = open_test_case(config)
    with pytest.raises((KeyError, TypeError, ValueError)):
        casefile.submit(case, {"garbage": True})
    reloaded = casefile.load_case(case.id)
    assert casefile.next_role(reloaded) == "bull"      # nothing was stored


def test_session_and_api_drivers_produce_identical_decisions(config):
    payloads = [thesis_payload(50.0), attack_payload(severe=True),
                verdict_payload("buy", 0.62, addressed=[])]

    # API driver
    rec_api = run_tribunal(make_brief(price=50.0), ScriptedLLM(list(payloads)),
                           config)

    # Session driver, same payloads
    case = open_test_case(config)
    for p in payloads:
        casefile.submit(case, p)
    rec_session = casefile.finalize(case, config)

    # identical outcome: unaddressed severe attack overturns BUY in both
    assert rec_api.verdict.decision == rec_session.verdict.decision \
        == DecisionType.PASS
    assert "RULE ENFORCEMENT" in rec_session.verdict.rationale
    assert rec_session.model == casefile.SESSION_MODEL_LABEL
    assert casefile.load_case(case.id).recommendation_id == rec_session.id


def test_finalize_requires_all_roles(config):
    case = open_test_case(config)
    casefile.submit(case, thesis_payload(50.0))
    with pytest.raises(ValueError):
        casefile.finalize(case, config)


def test_list_cases(config):
    case = open_test_case(config)
    ids = [c.id for c in casefile.list_cases()]
    assert case.id in ids
