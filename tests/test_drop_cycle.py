"""The review round: what gets recorded now, what waits for investigation,
and when a revision may be drafted (§5.2(3) [2][3][4])."""
from __future__ import annotations

from datetime import date

from hawkeye.contracts.models import DropReview, MissCategory
from hawkeye.scout import drop_cycle
from hawkeye.scout.drop_review import CheckpointResult


def _result(z: float, checkpoint: str = "t10", cohort: str = "GATE_REJECT",
            ticker: str = "ACME", scr: str | None = "scr_1",
            rec: str | None = None) -> CheckpointResult:
    return CheckpointResult(
        ticker=ticker, cohort=cohort, scan_id=1,
        decision_date=date(2026, 7, 1), checkpoint=checkpoint,
        checkpoint_date=date(2026, 7, 15), horizon_days=10,
        price_at_decision=50.0, price_at_checkpoint=55.0,
        raw_return_pct=10.0, benchmark_return_pct=1.0, beta=1.0,
        beta_window=250, atr_pct=3.0, alpha_pct=9.0, z=z,
        direction="up" if z >= 0 else "down",
        screened_candidate_id=scr, rec_id=rec)


# --- what waits and what is recorded straight away --------------------------

def test_t5_records_everything_and_investigates_nothing():
    """T+5 is a measurement, not an inquiry (token cost, and a name reviewed
    at both checkpoints would be counted twice in the 20-per-category tally)."""
    plan = drop_cycle.plan([_result(3.0), _result(0.2)], checkpoint="t5")
    assert len(plan.record_now) == 2
    assert plan.investigate == []


def test_t10_sends_only_the_outliers_to_investigation():
    plan = drop_cycle.plan([_result(2.0), _result(0.4)], checkpoint="t10")
    assert [r.z for r in plan.record_now] == [0.4]
    assert [r.z for r in plan.investigate] == [2.0]


def test_downside_outliers_are_investigated_too():
    """Reviewing only the ones that rose, then loosening the gate that
    dropped them, is a guaranteed way to make performance worse."""
    plan = drop_cycle.plan([_result(-2.5)], checkpoint="t10")
    assert [r.z for r in plan.investigate] == [-2.5]


def test_enrichment_cap_outliers_are_recorded_not_investigated():
    """Their only recorded reason is "ranked 16th by surprise" — reading one
    name cannot tell you what to change, so they stay in the aggregates and
    out of the queue."""
    plan = drop_cycle.plan([_result(3.0, cohort="ENRICHMENT_CAP")],
                           checkpoint="t10")
    assert plan.investigate == []
    assert len(plan.record_now) == 1


def test_already_recorded_subjects_are_not_measured_again():
    """"Measure it again and see" is how a disappointing number gets re-run
    until it flatters. The schema refuses the duplicate; the plan must not
    offer one in the first place."""
    plan = drop_cycle.plan(
        [_result(0.3, ticker="ACME", scr="scr_1"),
         _result(0.3, ticker="BETA", scr="scr_2")],
        checkpoint="t10",
        already_recorded={("scr_1", "", "t10")})
    assert [r.ticker for r in plan.record_now] == ["BETA"]
    assert plan.skipped_already_recorded == 1


def test_a_result_with_no_alpha_is_counted_as_unmeasurable():
    """Censored, not a verdict — storing it would put an empty judgement into
    the tallies a revision is drafted from."""
    censored = _result(0.0)
    censored = type(censored)(**{**censored.__dict__, "alpha_pct": None,
                                 "z": None, "direction": None})
    plan = drop_cycle.plan([censored], checkpoint="t10")
    assert plan.record_now == [] and plan.investigate == []
    assert plan.unmeasurable == 1


# --- the 20-per-category gate ----------------------------------------------

def _review(category: MissCategory, ticker: str = "ACME") -> DropReview:
    return DropReview(
        screened_candidate_id=f"scr_{ticker}", ticker=ticker,
        cohort="GATE_REJECT", checkpoint="t10", decision_date=date(2026, 7, 1),
        horizon_days=10, raw_return_pct=10.0, beta_window=250,
        alpha_pct=9.0, z=2.0, direction="up", miss_category=category)


def test_counts_ignore_reviews_that_were_never_investigated():
    """A measured-but-unexplained row has no cause attached, so it cannot
    argue for changing anything."""
    reviews = [_review(MissCategory.COLLECTION_GAP),
               DropReview(screened_candidate_id="scr_x", ticker="X",
                          cohort="GATE_REJECT", checkpoint="t5",
                          decision_date=date(2026, 7, 1), horizon_days=5,
                          raw_return_pct=1.0, beta_window=250, alpha_pct=1.0,
                          z=0.2, direction="up")]
    assert drop_cycle.category_counts(reviews) == {"collection_gap": 1}


def test_no_category_is_ready_below_the_threshold():
    reviews = [_review(MissCategory.COLLECTION_GAP, f"T{i}") for i in range(19)]
    assert drop_cycle.ready_categories(reviews, min_samples=20) == []


def test_a_category_becomes_ready_exactly_at_the_threshold():
    reviews = [_review(MissCategory.COLLECTION_GAP, f"T{i}") for i in range(20)]
    assert drop_cycle.ready_categories(reviews, min_samples=20) == [
        "collection_gap"]


def test_categories_are_counted_separately_not_pooled():
    """20 assorted misses do not identify a knob; 20 of the same cause do."""
    reviews = ([_review(MissCategory.COLLECTION_GAP, f"A{i}") for i in range(12)]
               + [_review(MissCategory.GATE_CORRECT, f"B{i}") for i in range(12)])
    assert drop_cycle.ready_categories(reviews, min_samples=20) == []


def test_remaining_shows_the_distance_to_the_threshold_for_every_category():
    """Reported every round, met or not — "0件見つかった" is only meaningful
    next to how many were looked at."""
    reviews = [_review(MissCategory.GATE_CORRECT, f"T{i}") for i in range(3)]
    remaining = drop_cycle.remaining_to_threshold(reviews, min_samples=20)
    assert remaining["gate_correct"] == 17
    assert remaining["collection_gap"] == 20   # untouched categories still listed
