"""End-to-end tribunal runs with a scripted LLM (fully offline)."""
import json

from hawkeye.contracts.models import Claim, DecisionType
from hawkeye.reports.render_ja import render_recommendation_ja
from hawkeye.tribunal.llm import ScriptedLLM
from hawkeye.tribunal.pipeline import (
    parse_attack_report,
    parse_thesis,
    run_tribunal,
)
from tests.conftest import (
    attack_payload,
    make_brief,
    thesis_payload,
    verdict_payload,
)


# --- claim ids (2026-08-01) -------------------------------------------------
# Claim ids used to be random uuids minted by the default factory, so every
# parse of the SAME bull output produced a different set. Session mode parses
# the thesis once per role package and once more at finalize, so the Adversary
# cited ids the Judge could not find and neither matched the ledger. Same bug
# class as the 2026-07-28 attack-id fix; same remedy.

def test_claim_ids_are_stable_across_repeated_parses():
    raw = thesis_payload(50.0)
    first = [c.id for c in parse_thesis(raw).claims]
    second = [c.id for c in parse_thesis(raw).claims]
    assert first == second
    assert all(cid.startswith("clm_") for cid in first)


def test_claim_id_is_derived_from_content_not_position():
    a = Claim(statement="X happens", probability=0.6, horizon_days=30,
              verification="check the 10-Q")
    same = Claim(statement="X happens", probability=0.6, horizon_days=30,
                 verification="check the 10-Q")
    different = Claim(statement="Y happens", probability=0.6, horizon_days=30,
                      verification="check the 10-Q")
    assert a.id == same.id
    assert a.id != different.id


def test_stored_claim_id_is_never_rewritten():
    """Invariant 1: a pre-registered payload is immutable. Records written
    before ids were content-derived must keep the id they were stored with."""
    legacy = Claim.model_validate({"id": "clm_legacyrandom", "statement": "X",
                                   "probability": 0.5, "horizon_days": 10,
                                   "verification": ""})
    assert legacy.id == "clm_legacyrandom"


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
                       verdict_payload("buy", 0.72)])
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
                       verdict_payload("buy", 0.72, addressed=[])])
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
                       verdict_payload("buy", 0.72, addressed=addressed)])
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
                       verdict_payload("buy", 0.72, addressed=addressed)])
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
                       verdict_payload("buy", 0.72, addressed=addressed)])
    rec = run_tribunal(brief, llm, config)
    assert rec.verdict.decision == DecisionType.PASS
    assert "RULE ENFORCEMENT" in rec.verdict.rationale


def test_low_conviction_buy_overturned(config):
    brief = make_brief(price=50.0)
    llm = ScriptedLLM([thesis_payload(50.0), attack_payload(),
                       verdict_payload("buy", 0.40)])
    rec = run_tribunal(brief, llm, config)
    assert rec.verdict.decision == DecisionType.PASS


# --- conviction floor, after the debate rule was folded into it ------------
# (T-001, 2026-08-17) The Judge used to carry a separate rule: "if the
# Adversary's short case is more convincing than the Bull's on the same facts,
# PASS." Converting a severe attack into a monitored kill criterion did not
# take it off those scales, so a surviving objection argued for PASS no matter
# what it was worth — 19 of the first 19 decisions came back PASS. That rule is
# gone; a converted-but-unrefuted severe attack is now paid for by discounting
# conviction, and the single mechanical gate is the floor, raised 0.55 -> 0.65.

def _converted_severe(severe: dict) -> list[dict]:
    """An `addressed` entry that CONVERTS the severe attack into a monitored
    risk instead of refuting it — the case the old rule always PASSed."""
    return [{
        "attack_id": parse_attack_report(severe).attacks[-1].id,
        "attack_statement": severe["attacks"][-1]["statement"],
        "response": "Not refutable from the record; carried as a kill "
                    "criterion — exit if the 10-Q shows the tax line drove it.",
        "converted_to_kill_criterion": True,
    }]


def test_converted_severe_attack_keeps_buy_above_the_floor(config):
    brief = make_brief(price=50.0)
    severe = attack_payload(severe=True)
    llm = ScriptedLLM([thesis_payload(50.0), severe,
                       verdict_payload("buy", 0.72,
                                       addressed=_converted_severe(severe))])
    rec = run_tribunal(brief, llm, config)
    assert rec.verdict.decision == DecisionType.BUY
    assert "RULE ENFORCEMENT" not in rec.verdict.rationale


def test_converted_severe_attack_overturns_buy_below_the_floor(config):
    brief = make_brief(price=50.0)
    severe = attack_payload(severe=True)
    llm = ScriptedLLM([thesis_payload(50.0), severe,
                       verdict_payload("buy", 0.60,
                                       addressed=_converted_severe(severe))])
    rec = run_tribunal(brief, llm, config)
    assert rec.verdict.decision == DecisionType.PASS
    assert "0.65" in rec.verdict.rationale


def test_conviction_that_cleared_the_old_floor_no_longer_buys(config):
    """0.62 was the fixtures' standard BUY conviction and cleared the old 0.55
    floor. The raise is only real if that exact number now fails."""
    brief = make_brief(price=50.0)
    llm = ScriptedLLM([thesis_payload(50.0), attack_payload(),
                       verdict_payload("buy", 0.62)])
    rec = run_tribunal(brief, llm, config)
    assert rec.verdict.decision == DecisionType.PASS


def test_conviction_exactly_at_the_floor_still_buys(config):
    """The floor is a minimum, not a threshold to exceed: the rule reads
    "below 0.65 is inconsistent", so 0.65 itself is a valid BUY."""
    brief = make_brief(price=50.0)
    llm = ScriptedLLM([thesis_payload(50.0), attack_payload(),
                       verdict_payload("buy", 0.65)])
    rec = run_tribunal(brief, llm, config)
    assert rec.verdict.decision == DecisionType.BUY


def test_risk_officer_veto_overturns_buy(config):
    brief = make_brief(price=50.0)
    thesis = thesis_payload(50.0)
    # make the base target barely above entry -> reward/risk collapses
    for s in thesis["scenarios"]:
        if s["name"] == "base":
            s["price_target"] = 51.0
    llm = ScriptedLLM([thesis, attack_payload(), verdict_payload("buy", 0.72)])
    rec = run_tribunal(brief, llm, config)
    assert rec.verdict.decision == DecisionType.PASS
    assert "RISK OFFICER VETO" in rec.verdict.rationale


def test_scenario_probabilities_normalized(config):
    brief = make_brief(price=50.0)
    thesis = thesis_payload(50.0)
    for s in thesis["scenarios"]:
        s["probability"] = s["probability"] * 2  # sums to 2.0
    llm = ScriptedLLM([thesis, attack_payload(), verdict_payload("buy", 0.72)])
    rec = run_tribunal(brief, llm, config)
    assert abs(sum(s.probability for s in rec.thesis.scenarios) - 1.0) < 1e-9


def test_adversary_and_judge_see_normalized_thesis_not_raw(config):
    """Adversary/Judge must argue over the same clamped/renormalized numbers
    that end up in the stored record, not the Bull's un-normalized raw
    output — otherwise the debated record and the stored record disagree."""
    brief = make_brief(price=50.0)
    thesis = thesis_payload(50.0)
    for s in thesis["scenarios"]:
        s["probability"] = s["probability"] * 2  # sums to 2.0, un-normalized
    llm = ScriptedLLM([thesis, attack_payload(), verdict_payload("buy", 0.72)])
    run_tribunal(brief, llm, config)

    adversary_payload = json.loads(llm.calls[1][1].split("\n\n", 1)[1])
    judge_payload = json.loads(llm.calls[2][1].split("\n\n", 1)[1])
    adv_probs = [s["probability"]
                for s in adversary_payload["thesis_under_attack"]["scenarios"]]
    judge_probs = [s["probability"]
                  for s in judge_payload["thesis"]["scenarios"]]
    assert abs(sum(adv_probs) - 1.0) < 1e-9
    assert abs(sum(judge_probs) - 1.0) < 1e-9


def test_japanese_report_renders_both_outcomes(config):
    brief = make_brief(price=50.0)
    llm = ScriptedLLM([thesis_payload(50.0), attack_payload(),
                       verdict_payload("buy", 0.72)])
    rec = run_tribunal(brief, llm, config)
    report = render_recommendation_ja(rec)
    assert "投資提案" in report and "キル基準" in report and "反証プロセス" in report

    llm2 = ScriptedLLM([thesis_payload(50.0), attack_payload(),
                        verdict_payload("pass", 0.3)])
    rec2 = run_tribunal(brief, llm2, config)
    assert "見送り" in render_recommendation_ja(rec2)
