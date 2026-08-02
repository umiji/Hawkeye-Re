"""The round report is emitted every time, threshold met or not.

A review process that only speaks when it has something to propose is
indistinguishable from one that stopped running. Each of these tests pins
down one thing the user asked to see on every round.
"""
from __future__ import annotations

from datetime import date

from hawkeye.contracts.models import DropReview, MissCategory, ProposedChange
from hawkeye.reports.render_ja import render_drop_cycle_ja


def _review(category=None, ticker="ACME", z=2.0, what="") -> DropReview:
    return DropReview(
        screened_candidate_id=f"scr_{ticker}", ticker=ticker,
        cohort="GATE_REJECT", checkpoint="t10", decision_date=date(2026, 7, 1),
        checkpoint_date=date(2026, 7, 15), horizon_days=10,
        raw_return_pct=12.0, beta_window=250, alpha_pct=9.0, z=z,
        direction="up" if z >= 0 else "down", miss_category=category,
        what_happened=what,
        visible_evidence=["時価総額が下限を下回っていた"] if what else [])


def _report(**over) -> str:
    base = dict(
        checkpoint="t10", measured=[], investigated=[],
        cohort_counts={"GATE_REJECT": 0},
        censored={}, pending=0, skipped=0,
        remaining={c.value: 20 for c in MissCategory},
        ready=[], min_samples=20, previous_total=0)
    base.update(over)
    return render_drop_cycle_ja(**base)


def test_a_round_that_found_nothing_says_so_rather_than_going_quiet():
    out = _report(measured=[_review(z=0.3) for _ in range(12)])
    assert "12" in out
    assert "該当なし" in out


def test_outliers_are_reported_in_both_directions():
    out = _report(measured=[_review(z=2.4), _review(z=-3.1, ticker="BETA")])
    assert "上振れ" in out and "下振れ" in out


def test_remaining_to_threshold_is_shown_even_with_nothing_ready():
    out = _report(remaining={**{c.value: 20 for c in MissCategory},
                             "collection_gap": 8},
                  ready=[])
    assert "collection_gap" in out or "収集の欠陥" in out
    assert "8" in out
    assert "改訂案は起草しません" in out


def test_a_ready_category_is_called_out():
    out = _report(remaining={**{c.value: 20 for c in MissCategory},
                             "collection_gap": 0},
                  ready=["collection_gap"])
    assert "改訂案" in out and "collection_gap" in out


def test_investigated_names_are_summarized_with_their_evidence():
    out = _report(investigated=[
        _review(MissCategory.COLLECTION_GAP, what="FDA承認が判断日前に出ていた")])
    assert "ACME" in out
    assert "FDA承認が判断日前に出ていた" in out
    assert "時価総額が下限を下回っていた" in out   # the decision-time quote


def test_unmeasurable_names_are_disclosed_with_the_reason():
    """Silently dropping the ones whose prices vanished biases every average
    upward — those are disproportionately the worst performers."""
    out = _report(censored={"GATE_REJECT": 3})
    assert "3" in out and "生存者バイアス" in out


def test_the_increment_since_last_round_is_stated():
    out = _report(measured=[_review(z=0.1) for _ in range(5)],
                  previous_total=40)
    assert "45" in out    # cumulative
    assert "+5" in out    # this round's addition
