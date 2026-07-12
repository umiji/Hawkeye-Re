from datetime import date, timedelta

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import (
    DecisionType,
    GateReport,
    Recommendation,
    Verdict,
)
from hawkeye.risk.sizing import build_position_plan
from hawkeye.sentinel.monitor import check_position
from hawkeye.tribunal.pipeline import parse_thesis
from tests.conftest import make_brief, thesis_payload


def open_rec(price=50.0) -> Recommendation:
    thesis = parse_thesis(thesis_payload(price=price))
    plan = build_position_plan(nav=100_000, entry_price=price,
                               stop_price=price * 0.93,
                               target_price=price * 1.12,
                               scenarios=thesis.scenarios,
                               config=HawkeyeConfig())
    return Recommendation(
        ticker="TEST", brief=make_brief(price=price), gate_report=GateReport(),
        thesis=thesis, plan=plan,
        verdict=Verdict(decision=DecisionType.BUY, conviction=0.6, rationale="t"))


def test_quiet_when_nothing_triggered():
    rec = open_rec()
    signals = check_position(rec, current_price=51.0, today=date(2026, 7, 10),
                             entry_date=date(2026, 7, 5))
    kinds = {s.kind for s in signals}
    assert "kill_stop" not in kinds and "kill_time" not in kinds
    # the EVENT kill criterion always asks for a manual confirmation
    assert "kill_event_review" in kinds


def test_stop_breach_fires_sell():
    rec = open_rec(price=50.0)
    signals = check_position(rec, current_price=46.0, today=date(2026, 7, 10),
                             entry_date=date(2026, 7, 5))
    assert any(s.kind == "kill_stop" and s.severity == "sell" for s in signals)


def test_time_stop_fires():
    rec = open_rec()
    signals = check_position(rec, current_price=51.0, today=date(2026, 8, 20),
                             entry_date=date(2026, 7, 1))
    assert any(s.kind == "kill_time" for s in signals)


def test_target_reached_fires_review():
    rec = open_rec(price=50.0)
    signals = check_position(rec, current_price=57.0, today=date(2026, 7, 10),
                             entry_date=date(2026, 7, 5))
    assert any(s.kind == "target_reached" and s.severity == "review"
               for s in signals)


def test_claim_deadline_prompts_resolution():
    rec = open_rec()
    signals = check_position(rec, current_price=51.0, today=date(2026, 7, 30),
                             entry_date=date(2026, 7, 1))  # 29d > 21d horizon
    assert any(s.kind == "claim_due" for s in signals)


def test_resolved_claims_stop_prompting():
    rec = open_rec()
    resolved = frozenset(c.id for c in rec.thesis.claims)
    signals = check_position(rec, current_price=51.0, today=date(2026, 7, 30),
                             entry_date=date(2026, 7, 1),
                             resolved_claim_ids=resolved)
    assert not any(s.kind == "claim_due" for s in signals)


def test_earnings_proximity_fires():
    rec = open_rec()
    rec.brief.snapshot.next_earnings_date = date(2026, 7, 12)
    signals = check_position(rec, current_price=51.0, today=date(2026, 7, 10),
                             entry_date=date(2026, 7, 5))
    assert any(s.kind == "earnings_near" for s in signals)
