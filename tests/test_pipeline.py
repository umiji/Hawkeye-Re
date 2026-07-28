"""End-to-end tribunal runs with a scripted LLM (fully offline)."""
from hawkeye.contracts.models import DecisionType
from hawkeye.reports.render_ja import render_recommendation_ja
from hawkeye.tribunal.llm import ScriptedLLM
from hawkeye.tribunal.pipeline import parse_attack_report, run_tribunal
from tests.conftest import (
    attack_payload,
    make_brief,
    thesis_payload,
    verdict_payload,
)


def test_hard_gate_failure_skips_llm(config):
    brief = make_brief(price=2.0)  # below min_price
    llm = ScriptedLLM([])          # any LLM call would raise
    rec = run_tribunal(brief, llm, config)
    assert rec.verdict.decision == DecisionType.PASS
    assert rec.thesis is None
    assert llm.calls == []


def test_full_buy_path(config):
    brief = make_brief(price=50.0)
    llm = ScriptedLLM([thesis_payload(50.0), attack_payload(),
                       verdict_payload("buy", 0.62)])
    rec = run_tribunal(brief, llm, config, nav=100_000)
    assert rec.verdict.decision == DecisionType.BUY
    assert rec.plan is not None and rec.plan.approved
    assert rec.plan.shares > 0
    assert rec.verdict.expected_value_pct is not None
    # information separation: adversary saw the thesis, bull did not see attacks
    assert "thesis_under_attack" in llm.calls[1][1]
    assert "attack" not in llm.calls[0][1].lower()


def test_unaddressed_severe_attack_overturns_buy(config):
    brief = make_brief(price=50.0)
    llm = ScriptedLLM([thesis_payload(50.0), attack_payload(severe=True),
                       verdict_payload("buy", 0.62, addressed=[])])
    rec = run_tribunal(brief, llm, config)
    assert rec.verdict.decision == DecisionType.PASS
    assert "RULE ENFORCEMENT" in rec.verdict.rationale


def test_addressed_severe_attack_keeps_buy(config):
    brief = make_brief(price=50.0)
    severe = attack_payload(severe=True)
    kill_shot_id = parse_attack_report(severe).attacks[-1].id
    addressed = [{
        "attack_id": kill_shot_id,
        "attack_statement": severe["attacks"][-1]["statement"],
        "response": "10-Q shows the beat is operating income; tax rate flat.",
        "converted_to_kill_criterion": False,
    }]
    llm = ScriptedLLM([thesis_payload(50.0), severe,
                       verdict_payload("buy", 0.62, addressed=addressed)])
    rec = run_tribunal(brief, llm, config)
    assert rec.verdict.decision == DecisionType.BUY


def test_addressed_attack_with_paraphrased_statement_keeps_buy(config):
    # Regression test for the substring-matching bug: a real judge
    # paraphrases the attack instead of quoting it verbatim. Only the
    # attack_id must matter — matching on statement text would wrongly
    # overturn this BUY even though the severe attack genuinely was
    # addressed.
    brief = make_brief(price=50.0)
    severe = attack_payload(severe=True)
    kill_shot_id = parse_attack_report(severe).attacks[-1].id
    addressed = [{
        "attack_id": kill_shot_id,
        "attack_statement": "The one-time-tax-benefit concern (paraphrased "
                            "in my own words, not quoted)",
        "response": "10-Q shows the beat is operating income; tax rate flat.",
        "converted_to_kill_criterion": False,
    }]
    llm = ScriptedLLM([thesis_payload(50.0), severe,
                       verdict_payload("buy", 0.62, addressed=addressed)])
    rec = run_tribunal(brief, llm, config)
    assert rec.verdict.decision == DecisionType.BUY


def test_addressed_attack_with_wrong_id_still_overturns_buy(config):
    # The inverse: a matching statement text but a WRONG/missing attack_id
    # must not save the BUY — id is the only thing that counts now.
    brief = make_brief(price=50.0)
    severe = attack_payload(severe=True)
    addressed = [{
        "attack_id": "atk_does_not_exist",
        "attack_statement": severe["attacks"][-1]["statement"],
        "response": "10-Q shows the beat is operating income; tax rate flat.",
        "converted_to_kill_criterion": False,
    }]
    llm = ScriptedLLM([thesis_payload(50.0), severe,
                       verdict_payload("buy", 0.62, addressed=addressed)])
    rec = run_tribunal(brief, llm, config)
    assert rec.verdict.decision == DecisionType.PASS
    assert "RULE ENFORCEMENT" in rec.verdict.rationale


def test_low_conviction_buy_overturned(config):
    brief = make_brief(price=50.0)
    llm = ScriptedLLM([thesis_payload(50.0), attack_payload(),
                       verdict_payload("buy", 0.40)])
    rec = run_tribunal(brief, llm, config)
    assert rec.verdict.decision == DecisionType.PASS


def test_risk_officer_veto_overturns_buy(config):
    brief = make_brief(price=50.0)
    thesis = thesis_payload(50.0)
    # make the base target barely above entry -> reward/risk collapses
    for s in thesis["scenarios"]:
        if s["name"] == "base":
            s["price_target"] = 51.0
    llm = ScriptedLLM([thesis, attack_payload(), verdict_payload("buy", 0.62)])
    rec = run_tribunal(brief, llm, config)
    assert rec.verdict.decision == DecisionType.PASS
    assert "RISK OFFICER VETO" in rec.verdict.rationale


def test_scenario_probabilities_normalized(config):
    brief = make_brief(price=50.0)
    thesis = thesis_payload(50.0)
    for s in thesis["scenarios"]:
        s["probability"] = s["probability"] * 2  # sums to 2.0
    llm = ScriptedLLM([thesis, attack_payload(), verdict_payload("buy", 0.62)])
    rec = run_tribunal(brief, llm, config)
    assert abs(sum(s.probability for s in rec.thesis.scenarios) - 1.0) < 1e-9


def test_japanese_report_renders_both_outcomes(config):
    brief = make_brief(price=50.0)
    llm = ScriptedLLM([thesis_payload(50.0), attack_payload(),
                       verdict_payload("buy", 0.62)])
    rec = run_tribunal(brief, llm, config)
    report = render_recommendation_ja(rec)
    assert "投資提案" in report and "キル基準" in report and "反証プロセス" in report

    llm2 = ScriptedLLM([thesis_payload(50.0), attack_payload(),
                        verdict_payload("pass", 0.3)])
    rec2 = run_tribunal(brief, llm2, config)
    assert "見送り" in render_recommendation_ja(rec2)
