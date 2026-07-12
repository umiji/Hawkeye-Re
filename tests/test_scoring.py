from hawkeye.contracts.models import OutcomeQuadrant
from hawkeye.ledger.scoring import (
    brier_score,
    calibration_table,
    classify_outcome,
    thesis_accuracy,
)


def test_brier_perfect_and_coinflip():
    assert brier_score([(1.0, True), (0.0, False)]) == 0.0
    assert abs(brier_score([(0.5, True), (0.5, False)]) - 0.25) < 1e-9
    assert brier_score([]) is None


def test_thesis_accuracy():
    assert thesis_accuracy([(0.7, True), (0.6, True), (0.8, False)]) == 2 / 3
    assert thesis_accuracy([]) is None


def test_quadrants():
    assert classify_outcome(5.0, 0.8) == OutcomeQuadrant.SKILL_WIN
    assert classify_outcome(5.0, 0.3) == OutcomeQuadrant.LUCKY_WIN
    assert classify_outcome(-5.0, 0.8) == OutcomeQuadrant.UNLUCKY_LOSS
    assert classify_outcome(-5.0, 0.3) == OutcomeQuadrant.DESERVED_LOSS
    assert classify_outcome(5.0, None) is None


def test_calibration_table_buckets():
    pairs = [(0.6, True), (0.65, False), (0.9, True), (0.9, True)]
    rows = calibration_table(pairs)
    mid = next(r for r in rows if r["band"] == "0.50-0.70")
    assert mid["n"] == 2 and mid["freq_true"] == 0.5
    high = next(r for r in rows if r["band"] == "0.85-1.00")
    assert high["n"] == 2 and high["freq_true"] == 1.0
