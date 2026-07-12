from datetime import date

from hawkeye.contracts.models import (
    DecisionType,
    GateReport,
    Recommendation,
    RecommendationStatus,
    Verdict,
)
from hawkeye.ledger.store import Ledger
from hawkeye.tribunal.pipeline import parse_thesis
from tests.conftest import make_brief, thesis_payload


def make_rec() -> Recommendation:
    return Recommendation(
        ticker="TEST",
        brief=make_brief(),
        gate_report=GateReport(),
        thesis=parse_thesis(thesis_payload()),
        verdict=Verdict(decision=DecisionType.BUY, conviction=0.6,
                        rationale="test"),
    )


def test_roundtrip_and_lifecycle(tmp_path):
    ledger = Ledger(str(tmp_path / "test.db"))
    rec = make_rec()
    ledger.record_recommendation(rec, RecommendationStatus.PROPOSED)

    loaded = ledger.get(rec.id)
    assert loaded is not None and loaded.ticker == "TEST"
    assert loaded.thesis is not None and len(loaded.thesis.claims) == 3

    ledger.record_decision(rec.id, approved=True, note="ok")
    assert ledger.status(rec.id) == RecommendationStatus.APPROVED

    ledger.record_entry(rec.id, price=50.5, shares=100,
                        trade_date=date(2026, 7, 1))
    assert ledger.status(rec.id) == RecommendationStatus.OPEN
    assert len(ledger.open_positions()) == 1

    ledger.record_exit(rec.id, price=55.0, trade_date=date(2026, 7, 20))
    assert ledger.status(rec.id) == RecommendationStatus.CLOSED
    assert ledger.entry(rec.id)["price"] == 50.5
    assert ledger.exit(rec.id)["price"] == 55.0


def test_claim_resolution_via_journal_not_mutation(tmp_path):
    ledger = Ledger(str(tmp_path / "test.db"))
    rec = make_rec()
    ledger.record_recommendation(rec, RecommendationStatus.PROPOSED)
    claim_id = rec.thesis.claims[0].id

    ledger.resolve_claim(rec.id, claim_id, outcome=True, note="verified")
    # payload untouched (pre-registration integrity)...
    reloaded = ledger.get(rec.id)
    assert reloaded.model_dump_json() == rec.model_dump_json()
    # ...resolution lives in the journal
    assert ledger.claim_resolutions(rec.id)[claim_id] == (True, "verified")


def test_hash_chain_detects_tampering(tmp_path):
    path = str(tmp_path / "test.db")
    ledger = Ledger(path)
    rec = make_rec()
    ledger.record_recommendation(rec, RecommendationStatus.PROPOSED)
    ledger.record_decision(rec.id, approved=False)
    assert ledger.verify_chain()

    # simulate someone rewriting history
    ledger._conn.execute(
        "UPDATE journal SET payload = ? WHERE kind = 'user_decision'",
        ('{"approved": true, "note": ""}',))
    ledger._conn.commit()
    assert not ledger.verify_chain()


def test_all_resolved_claims_aggregation(tmp_path):
    ledger = Ledger(str(tmp_path / "test.db"))
    rec = make_rec()
    ledger.record_recommendation(rec, RecommendationStatus.PROPOSED)
    ledger.resolve_claim(rec.id, rec.thesis.claims[0].id, True)
    ledger.resolve_claim(rec.id, rec.thesis.claims[1].id, False)
    pairs = ledger.all_resolved_claims()
    assert len(pairs) == 2
    assert (0.7, True) in pairs and (0.65, False) in pairs
