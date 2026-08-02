"""Persistence for drop-candidate reviews (docs/MASTER_OVERVIEW.ja.md §5.2(3)).

The measurement half (`hawkeye/scout/drop_review.py`) recomputes alpha and z
on every run and throws them away. That is fine for a report and useless for
everything the design asks the reviews to support:

- the Agent's investigation ([3]) is not recomputable — unsaved means gone;
- [4] only fires once the same `miss_category` has accumulated enough rows,
  so there has to be something to count;
- beta is regressed over a rolling window and past prices are rewritten by
  splits and dividends, so "re-run it and see" does not reproduce the call
  that was made. The inputs have to be frozen with the verdict.

A review is a *later, separate* event from the drop it reviews, so it gets
its own table rather than extra columns on `screened_candidates` — writing
into that record would be the same class of rewrite invariant 1 forbids on
recommendation payloads.
"""
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from hawkeye.contracts.models import DropReview, MissCategory, ProposedChange
from hawkeye.ledger.store import Ledger


def make_review(ticker="DROP", checkpoint="t5",
                screened_candidate_id="scr_aaa", **overrides) -> DropReview:
    base = dict(
        screened_candidate_id=screened_candidate_id,
        scan_id=1,
        ticker=ticker,
        cohort="GATE_REJECT",
        checkpoint=checkpoint,
        decision_date=date(2026, 7, 29),
        checkpoint_date=date(2026, 8, 5),
        horizon_days=5,
        price_at_decision=100.0,
        price_at_checkpoint=112.0,
        raw_return_pct=12.0,
        benchmark_return_pct=1.0,
        beta=1.2,
        beta_window=250,
        atr_pct=3.0,
        alpha_pct=10.8,
        z=1.61,
        direction="up",
    )
    base.update(overrides)
    return DropReview(**base)


# --- the record itself ------------------------------------------------------

def test_a_measurement_only_review_is_valid_before_any_investigation():
    """[2] produces the numbers; [3] adds the story later. A row that has
    only the numbers must be storable, or there is nothing for the Agent's
    investigation queue to attach itself to."""
    review = make_review()

    assert review.miss_category is None
    assert review.what_happened == ""
    assert review.proposed_change is None


def test_a_review_must_point_at_the_decision_it_reviews():
    """Without a link back to the screened candidate or the recommendation,
    the row cannot be joined to what was visible at drop time — which is the
    only thing that keeps the investigation from being hindsight fiction."""
    with pytest.raises(ValidationError):
        make_review(screened_candidate_id=None, rec_id=None)


def test_other_category_requires_notes():
    """§5.2(3): `other` is the escape hatch, and an unexplained escape hatch
    silently becomes the biggest bucket. The design makes notes mandatory;
    invariant 3 says the code enforces what the design asks for."""
    with pytest.raises(ValidationError):
        make_review(miss_category=MissCategory.OTHER, notes="")

    assert make_review(miss_category=MissCategory.OTHER,
                       notes="acquired mid-window").notes


def test_checkpoint_is_limited_to_the_two_fixed_observation_points():
    """"T+5営業日 → T+10営業日で確定、以降は再チェックしない" — a horizon
    invented at write time would let a disappointing result be re-measured
    until it flatters."""
    with pytest.raises(ValidationError):
        make_review(checkpoint="t30")


# --- storage ----------------------------------------------------------------

def test_record_and_read_back_drop_reviews(tmp_path):
    ledger = Ledger(str(tmp_path / "test.db"))
    rows = [make_review(ticker="A", screened_candidate_id="scr_a"),
            make_review(ticker="B", screened_candidate_id="scr_b"),
            make_review(ticker="C", screened_candidate_id="scr_c",
                        checkpoint="t10")]

    ledger.record_drop_reviews(rows)

    assert {r.ticker for r in ledger.drop_reviews()} == {"A", "B", "C"}
    assert [r.ticker for r in ledger.drop_reviews(checkpoint="t10")] == ["C"]
    assert ledger.verify_chain()


def test_every_reproduction_input_survives_the_round_trip(tmp_path):
    """Alpha and z alone cannot be audited later: beta comes from a rolling
    regression and split/dividend adjustments rewrite the prices it was
    estimated from."""
    ledger = Ledger(str(tmp_path / "test.db"))
    original = make_review(
        miss_category=MissCategory.GATE_THRESHOLD_TOO_STRICT,
        what_happened="guidance raise the screen never saw",
        visible_evidence=["gate_report.liquidity.value=0.9"],
        evidence_urls=["https://example.com/pr"],
        proposed_change=ProposedChange(
            target="config.min_avg_dollar_volume",
            direction="loosen",
            rationale="8 rejects clustered just under the line"),
        confidence=0.4,
        reviewer_model="claude-opus-4-8")

    ledger.record_drop_reviews([original])

    stored = ledger.drop_reviews()[0]
    assert stored == original


def test_reviews_can_be_filtered_by_miss_category(tmp_path):
    """[4] fires on "the same category has reached N" — counting it must not
    require deserializing the whole table."""
    ledger = Ledger(str(tmp_path / "test.db"))
    ledger.record_drop_reviews([
        make_review(ticker="A", screened_candidate_id="scr_a",
                    miss_category=MissCategory.GATE_THRESHOLD_TOO_STRICT),
        make_review(ticker="B", screened_candidate_id="scr_b",
                    miss_category=MissCategory.GATE_CORRECT),
        make_review(ticker="C", screened_candidate_id="scr_c",
                    miss_category=MissCategory.GATE_THRESHOLD_TOO_STRICT),
    ])

    hits = ledger.drop_reviews(miss_category="gate_threshold_too_strict")

    assert {r.ticker for r in hits} == {"A", "C"}


# --- write-once discipline --------------------------------------------------

def test_a_candidate_cannot_be_reviewed_twice_at_the_same_checkpoint(tmp_path):
    """The checkpoint is fixed by design and never re-checked. Allowing a
    second row for the same (candidate, checkpoint) would reintroduce exactly
    the re-run-until-it-flatters loop the fixed horizons exist to prevent."""
    ledger = Ledger(str(tmp_path / "test.db"))
    ledger.record_drop_reviews([make_review(screened_candidate_id="scr_a")])

    with pytest.raises(ValueError):
        ledger.record_drop_reviews([
            make_review(screened_candidate_id="scr_a", alpha_pct=99.0)])

    assert [r.alpha_pct for r in ledger.drop_reviews()] == [10.8]
    assert ledger.verify_chain()


def test_the_same_candidate_is_reviewed_at_both_checkpoints(tmp_path):
    ledger = Ledger(str(tmp_path / "test.db"))
    ledger.record_drop_reviews([make_review(screened_candidate_id="scr_a")])
    ledger.record_drop_reviews([
        make_review(screened_candidate_id="scr_a", checkpoint="t10")])

    assert {r.checkpoint for r in ledger.drop_reviews()} == {"t5", "t10"}
    assert ledger.verify_chain()


def test_a_rejected_batch_leaves_no_partial_rows(tmp_path):
    """A half-written batch would leave a journal-anchored batch hash that
    no longer matches its rows, i.e. a permanently un-verifiable chain."""
    ledger = Ledger(str(tmp_path / "test.db"))
    ledger.record_drop_reviews([make_review(screened_candidate_id="scr_a")])

    with pytest.raises(ValueError):
        ledger.record_drop_reviews([
            make_review(ticker="NEW", screened_candidate_id="scr_new"),
            make_review(ticker="DUP", screened_candidate_id="scr_a"),
        ])

    assert [r.ticker for r in ledger.drop_reviews()] == ["DROP"]
    assert ledger.verify_chain()


# --- tamper evidence --------------------------------------------------------

def test_verify_chain_detects_a_rewritten_review(tmp_path):
    """Same gap, same fix as recommendations and screened_candidates: the
    batch hash anchored in the (chained) journal must still match the rows."""
    ledger = Ledger(str(tmp_path / "test.db"))
    original = make_review()
    ledger.record_drop_reviews([original])
    assert ledger.verify_chain()

    tampered = original.model_copy(update={"alpha_pct": 99.0}).model_dump_json()
    ledger._conn.execute("UPDATE drop_reviews SET payload = ?", (tampered,))
    ledger._conn.commit()

    assert not ledger.verify_chain()


def test_verify_chain_detects_a_deleted_review(tmp_path):
    """Deleting the inconvenient half of a batch is the cheapest way to make
    a review look better than it was."""
    ledger = Ledger(str(tmp_path / "test.db"))
    ledger.record_drop_reviews([
        make_review(ticker="A", screened_candidate_id="scr_a"),
        make_review(ticker="B", screened_candidate_id="scr_b")])
    assert ledger.verify_chain()

    ledger._conn.execute("DELETE FROM drop_reviews WHERE ticker = 'B'")
    ledger._conn.commit()

    assert not ledger.verify_chain()


# --- conversion from the measurement half -----------------------------------

def test_a_checkpoint_result_converts_into_a_storable_review(tmp_path):
    """`drops report` already computes every reproduction input; the store
    must be fed from that exact object rather than a re-measurement."""
    from hawkeye.scout.drop_review import CheckpointResult, to_drop_review

    result = CheckpointResult(
        ticker="ZZZ", cohort="GATE_REJECT", scan_id=7,
        decision_date=date(2026, 7, 29), checkpoint="t5",
        checkpoint_date=date(2026, 8, 5), horizon_days=5,
        price_at_decision=100.0, price_at_checkpoint=112.0,
        raw_return_pct=12.0, benchmark_return_pct=1.0, beta=1.2,
        beta_window=250, atr_pct=3.0, alpha_pct=10.8, z=1.61,
        direction="up", screened_candidate_id="scr_zzz",
        reject_reason="liquidity", failed_gates=("liquidity",), score=8.5)

    review = to_drop_review(result, reviewer_model="claude-opus-4-8")

    assert review.ticker == "ZZZ"
    assert review.scan_id == 7
    assert review.screened_candidate_id == "scr_zzz"
    assert (review.beta, review.beta_window, review.atr_pct) == (1.2, 250, 3.0)
    assert (review.price_at_decision, review.price_at_checkpoint) == (100.0, 112.0)
    assert (review.alpha_pct, review.z, review.direction) == (10.8, 1.61, "up")
    assert review.reviewer_model == "claude-opus-4-8"

    ledger = Ledger(str(tmp_path / "test.db"))
    ledger.record_drop_reviews([review])
    assert ledger.drop_reviews()[0] == review
    assert ledger.verify_chain()


def test_conversion_refuses_a_result_that_was_never_scored():
    """A row with no alpha is a censored fetch, not a verdict. Storing it as
    a review would put an empty judgement into the counts [4] acts on."""
    from hawkeye.scout.drop_review import CheckpointResult, to_drop_review

    unscored = CheckpointResult(
        ticker="ZZZ", cohort="GATE_REJECT", scan_id=7,
        decision_date=date(2026, 7, 29), checkpoint="t5",
        checkpoint_date=None, horizon_days=5, price_at_decision=None,
        price_at_checkpoint=None, raw_return_pct=0.0,
        benchmark_return_pct=None, beta=None, beta_window=250, atr_pct=None,
        alpha_pct=None, z=None, direction=None,
        screened_candidate_id="scr_zzz")

    with pytest.raises(ValueError):
        to_drop_review(unscored)


def test_recorded_subjects_are_reportable_so_a_round_never_re_measures(tmp_path):
    """The unique index refuses a duplicate; the caller needs to know which
    subjects are already done *before* it spends an API call re-fetching
    prices for them. Without this, every review round would re-measure the
    entire back catalogue and then abort on the first insert."""
    ledger = Ledger(str(tmp_path / "test.db"))
    ledger.record_drop_reviews([
        make_review(ticker="AAA", checkpoint="t5", screened_candidate_id="scr_a"),
        make_review(ticker="BBB", checkpoint="t10", screened_candidate_id="scr_b"),
    ])

    # Absent ids come back as "" — the same convention the unique index
    # relies on, since SQLite treats each NULL in an index as distinct.
    assert ledger.recorded_drop_review_keys() == {
        ("scr_a", "", "t5"), ("scr_b", "", "t10")}
    assert ledger.recorded_drop_review_keys("t5") == {("scr_a", "", "t5")}
