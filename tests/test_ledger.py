import hashlib
import threading
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


def test_append_event_atomic_under_concurrent_writers(tmp_path):
    """Two processes appending to the same journal must not corrupt the
    hash chain. append_event's read-prev-hash-then-insert used to be two
    separate statements; two connections could both read the same prev_hash
    and each insert a row claiming it, breaking verify_chain() with no way
    to recover the true order after the fact."""
    path = str(tmp_path / "test.db")
    Ledger(path)  # create schema once, up front

    n_threads, events_per_thread = 6, 15
    errors: list[Exception] = []

    def worker(worker_id: int) -> None:
        try:
            ledger = Ledger(path)
            for i in range(events_per_thread):
                ledger.append_event(f"rec-{worker_id}", "sentinel_signal",
                                    {"worker": worker_id, "seq": i})
            ledger.close()
        except Exception as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    verifier = Ledger(path)
    assert verifier.verify_chain()
    total_events = sum(len(verifier.events(f"rec-{w}")) for w in range(n_threads))
    assert total_events == n_threads * events_per_thread


def test_verify_chain_detects_rewritten_recommendation_row(tmp_path):
    """A tamperer who rewrites recommendations.payload directly via SQL and
    updates its own `hash` column to match (so that column looks internally
    consistent) must still be caught — only the journal's payload_hash,
    itself protected by the hash chain, is a trustworthy reference."""
    path = str(tmp_path / "test.db")
    ledger = Ledger(path)
    rec = make_rec()
    ledger.record_recommendation(rec, RecommendationStatus.PROPOSED)
    assert ledger.verify_chain()

    tampered_payload = rec.model_copy(update={"ticker": "EVIL"}).model_dump_json()
    ledger._conn.execute(
        "UPDATE recommendations SET payload = ?, hash = ? WHERE id = ?",
        (tampered_payload,
         hashlib.sha256(tampered_payload.encode("utf-8")).hexdigest(),
         rec.id))
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
