"""Deleting settled hold rows (docs/backlog/PIPELINE_BUILD_TASKS.ja.md task 6.5).

A print whose own figures have not arrived is HELD, and every scan writes one
more `actual_pending` row for it. Once the print settles, those rows are noise
in a table whose whole purpose is to be read later. Task 6.5 asks for a way to
remove them.

The hard part is not the DELETE. `screened_candidates` is anchored in the
hash-chained journal — one event per scan carrying that scan's row count and a
hash of its payloads (`record_screened_candidates`) — so removing a row makes
`verify_chain()` report tampering, forever, on a ledger nobody tampered with.
Invariant 2 says that check stays green.

So a purge is itself a journal event. The chain then says something stronger
than "nothing was ever deleted": it says **nothing was deleted without the
deletion being recorded**. A row removed by hand, outside the command, still
fails verification — and the test at the bottom proves it does.

Three things are refused outright rather than warned about:

- rows a drop-candidate review points at. The review's whole value is the join
  back to what was visible at drop time; delete the row and the verdict floats.
- `actual_timeout` rows. "The data never came" is a measurement, and it is one
  of the inputs the drop review reads.
- rows still waiting. A hold with no successor is live, not settled.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from hawkeye.contracts.models import (
    DropReview,
    ScreenedCandidate,
    ScreenedCandidateStage,
)
from hawkeye.ledger.purge import plan_purge
from hawkeye.ledger.store import Ledger

DAY = date(2026, 8, 3)
T0 = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)


def a_candidate(ticker="AAA", stage=ScreenedCandidateStage.ACTUAL_PENDING,
                scan_id=1, at=T0, **overrides) -> ScreenedCandidate:
    base = dict(scan_id=scan_id, ticker=ticker, event_date=DAY,
                recorded_at=at, eps_surprise_pct=8.0, score=8.0,
                score_version="partial_no_gap", stage=stage)
    base.update(overrides)
    return ScreenedCandidate(**base)


def a_review(candidate_id: str) -> DropReview:
    return DropReview(
        screened_candidate_id=candidate_id, scan_id=1, ticker="AAA",
        cohort="GATE_REJECT", checkpoint="t5", decision_date=DAY,
        checkpoint_date=DAY + timedelta(days=5), horizon_days=5,
        price_at_decision=100.0, price_at_checkpoint=112.0,
        raw_return_pct=12.0, benchmark_return_pct=1.0, beta=1.2,
        beta_window=250, atr_pct=3.0, alpha_pct=10.8, z=1.61, direction="up")


def a_ledger(tmp_path) -> Ledger:
    return Ledger(str(tmp_path / "hawkeye.db"))


# --- what counts as settled ------------------------------------------------

def test_a_hold_that_was_later_judged_is_removable():
    """The same print reappears at a stage past the hold, so the wait is over
    and the hold rows are the trail it left behind."""
    held = a_candidate(at=T0)
    judged = a_candidate(at=T0 + timedelta(days=1), scan_id=2,
                         stage=ScreenedCandidateStage.GATE_REJECT)

    plan = plan_purge([held, judged], reviews=[])

    assert [c.id for c in plan.removable] == [held.id]


def test_a_hold_still_waiting_is_kept():
    """No successor row means the print has not settled. Deleting it would
    throw away the only record that the system is waiting on this name."""
    plan = plan_purge([a_candidate()], reviews=[])

    assert plan.removable == []
    assert plan.protected[0][1] == "still_waiting"


def test_a_timeout_row_is_never_removable():
    """"The figures never arrived" is a measured fact and an input to the drop
    review, not a leftover."""
    timed_out = a_candidate(stage=ScreenedCandidateStage.ACTUAL_TIMEOUT)

    plan = plan_purge([timed_out], reviews=[])

    assert plan.removable == []
    assert plan.protected[0][1] == "timeout_is_a_measurement"


def test_a_row_a_review_points_at_is_never_removable():
    """Even settled. The review's verdict is only auditable through the row
    that says what was visible when the candidate was dropped."""
    held = a_candidate(at=T0)
    judged = a_candidate(at=T0 + timedelta(days=1), scan_id=2,
                         stage=ScreenedCandidateStage.GATE_REJECT)

    plan = plan_purge([held, judged], reviews=[a_review(held.id)])

    assert plan.removable == []
    assert plan.protected[0][1] == "referenced_by_drop_review"


def test_only_the_named_stage_and_window_are_considered():
    """Period and stage filters, as task 6.5 asks for."""
    old = a_candidate(at=T0)
    recent = a_candidate(at=T0 + timedelta(days=10), scan_id=2)
    judged = a_candidate(at=T0 + timedelta(days=20), scan_id=3,
                         stage=ScreenedCandidateStage.GATE_REJECT)

    plan = plan_purge([old, recent, judged], reviews=[],
                      before=date(2026, 8, 10))

    assert [c.id for c in plan.removable] == [old.id]


# --- the chain stays green -------------------------------------------------

def test_a_recorded_purge_leaves_the_chain_verifiable(tmp_path):
    ledger = a_ledger(tmp_path)
    held = a_candidate(at=T0)
    judged = a_candidate(at=T0 + timedelta(days=1), stage=
                         ScreenedCandidateStage.GATE_REJECT)
    ledger.record_screened_candidates(1, [held, judged])
    assert ledger.verify_chain()

    removed = ledger.purge_screened_candidates([held.id])

    assert removed == 1
    assert ledger.verify_chain()
    assert [c.id for c in ledger.screened_candidates()] == [judged.id]


def test_the_purge_is_itself_a_journal_event(tmp_path):
    """Removing a row is a fact about the ledger, so it goes in the ledger."""
    ledger = a_ledger(tmp_path)
    held = a_candidate(at=T0)
    ledger.record_screened_candidates(1, [held, a_candidate("BBB", at=T0)])
    ledger.purge_screened_candidates([held.id])

    events = [e for e in ledger.events("1")
              if e["kind"] == "screened_candidates_purged"]

    assert len(events) == 1
    assert events[0]["payload"]["removed_ids"] == [held.id]


def test_a_row_deleted_outside_the_command_still_fails_verification(tmp_path):
    """The point of journalling the purge is NOT to make deletion invisible to
    the chain — it is to make an UNRECORDED deletion the only thing that
    trips it."""
    ledger = a_ledger(tmp_path)
    held = a_candidate(at=T0)
    ledger.record_screened_candidates(1, [held, a_candidate("BBB", at=T0)])

    ledger._conn.execute("DELETE FROM screened_candidates WHERE id = ?",
                         (held.id,))
    ledger._conn.commit()

    assert ledger.verify_chain() is False


def test_the_command_previews_by_default_and_needs_apply_to_delete(
        tmp_path, monkeypatch, capsys):
    """Destructive by request only. A delete command whose default run
    deletes is one typo away from removing a ledger's history."""
    from hawkeye.cli import main

    db = str(tmp_path / "hawkeye.db")
    monkeypatch.setenv("HAWKEYE_DB", db)
    held = a_candidate(at=T0)
    judged = a_candidate(at=T0 + timedelta(days=1), scan_id=2,
                         stage=ScreenedCandidateStage.GATE_REJECT)
    ledger = Ledger(db)
    ledger.record_screened_candidates(1, [held])
    ledger.record_screened_candidates(2, [judged])
    ledger.close()

    assert main(["screened", "purge"]) == 0
    preview = capsys.readouterr().out
    assert "--apply" in preview and held.ticker in preview
    assert len(Ledger(db).screened_candidates()) == 2

    assert main(["screened", "purge", "--apply"]) == 0
    assert [c.id for c in Ledger(db).screened_candidates()] == [judged.id]
    assert Ledger(db).verify_chain()


def test_the_command_says_what_it_spared_and_why(tmp_path, monkeypatch,
                                                 capsys):
    from hawkeye.cli import main

    db = str(tmp_path / "hawkeye.db")
    monkeypatch.setenv("HAWKEYE_DB", db)
    ledger = Ledger(db)
    ledger.record_screened_candidates(
        1, [a_candidate(at=T0),
            a_candidate("TIME", at=T0,
                        stage=ScreenedCandidateStage.ACTUAL_TIMEOUT)])
    ledger.close()

    main(["screened", "purge"])
    out = capsys.readouterr().out

    assert "実績が届くのを待っている" in out
    assert "実績が来なかったという実測" in out


def test_purging_a_row_a_review_points_at_is_refused_at_the_store(tmp_path):
    """The plan above is advice; this is the wall. A caller that skips the
    planner must not be able to orphan a review."""
    ledger = a_ledger(tmp_path)
    held = a_candidate(at=T0)
    ledger.record_screened_candidates(1, [held, a_candidate("BBB", at=T0)])
    ledger.record_drop_reviews([a_review(held.id)])

    with pytest.raises(ValueError, match="drop review"):
        ledger.purge_screened_candidates([held.id])

    assert ledger.verify_chain()
