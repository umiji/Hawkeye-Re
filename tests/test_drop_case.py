"""Investigation staging for drop reviews (§5.2(3) [3]).

What these tests pin down is the anti-hindsight machinery. An investigating
agent asked "why did this one run away from us?" will always produce a
plausible story; the only defense is to control what it can see and to
refuse the one verdict that ends the inquiry ("nobody could have known")
whenever the record shows otherwise.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from hawkeye.contracts.models import MissCategory, NewsItem
from hawkeye.scout import drop_case
from hawkeye.scout.drop_review import CheckpointResult


DECISION = date(2026, 7, 1)


def _result(**over) -> CheckpointResult:
    base = dict(
        ticker="ACME", cohort="GATE_REJECT", scan_id=7,
        decision_date=DECISION, checkpoint="t10",
        checkpoint_date=date(2026, 7, 15), horizon_days=10,
        price_at_decision=50.0, price_at_checkpoint=60.0,
        raw_return_pct=20.0, benchmark_return_pct=1.0, beta=1.1,
        beta_window=250, atr_pct=3.0, alpha_pct=18.9, z=2.0,
        direction="up", screened_candidate_id="scr_1",
        reject_reason="market cap below floor",
        failed_gates=("market_cap",))
    base.update(over)
    return CheckpointResult(**base)


def _news(headline: str, day: date, url: str = "") -> NewsItem:
    return NewsItem(headline=headline, url=url or f"https://x/{headline}",
                    published_at=datetime(day.year, day.month, day.day,
                                          12, 0, tzinfo=timezone.utc))


# --- the cutoff is mechanical, not an instruction ---------------------------

def test_articles_published_after_the_decision_are_dropped_in_code():
    """The agent must never receive them, so it cannot cite them by accident.

    Telling a model "don't use information from after the decision" is the
    same class of guarantee as telling the Bull not to peek at the attack
    report — worth nothing without the file boundary behind it.
    """
    fetched = [
        _news("guidance raised", date(2026, 6, 29)),
        _news("takeover rumor", date(2026, 7, 4)),      # after the decision
    ]
    kept, excluded = drop_case.published_before(fetched, DECISION)
    assert [n.headline for n in kept] == ["guidance raised"]
    assert excluded == 1


def test_an_article_published_on_the_decision_day_counts_as_visible():
    fetched = [_news("same day", DECISION)]
    kept, excluded = drop_case.published_before(fetched, DECISION)
    assert len(kept) == 1 and excluded == 0


def test_undated_articles_are_excluded_rather_than_assumed_visible():
    """No publish date means we cannot prove it predates the decision, and
    the whole point of this split is which side carries the burden."""
    kept, excluded = drop_case.published_before(
        [NewsItem(headline="undated", url="https://x/u")], DECISION)
    assert kept == [] and excluded == 1


# --- what we had vs what was out there --------------------------------------

def test_missed_items_are_the_ones_we_never_collected():
    ours = [_news("earnings beat", date(2026, 6, 28))]
    theirs = [_news("earnings beat", date(2026, 6, 28)),
              _news("new FDA clearance", date(2026, 6, 30))]
    missed = drop_case.missed_items(ours, theirs)
    assert [n.headline for n in missed] == ["new FDA clearance"]


def test_same_story_from_a_different_url_is_not_counted_as_missed():
    """Syndicated copies would otherwise manufacture a collection gap on
    every single name, which would swamp the real ones."""
    ours = [_news("Acme beats and raises", date(2026, 6, 28),
                  url="https://a/1")]
    theirs = [_news("ACME  Beats and Raises!", date(2026, 6, 28),
                    url="https://b/2")]
    assert drop_case.missed_items(ours, theirs) == []


# --- the package the investigator sees --------------------------------------

def test_package_carries_both_sides_and_never_the_excluded_ones():
    case = drop_case.open_case(
        _result(),
        record_at_decision=[_news("earnings beat", date(2026, 6, 28))],
        refetched=[_news("earnings beat", date(2026, 6, 28)),
                   _news("new FDA clearance", date(2026, 6, 30)),
                   _news("takeover rumor", date(2026, 7, 4))])
    package = drop_case.render_input(case)
    assert "new FDA clearance" in package
    assert "takeover rumor" not in package
    assert "1件" in package  # the excluded count is disclosed, not hidden


def test_package_states_the_move_without_naming_a_cause():
    case = drop_case.open_case(_result(), record_at_decision=[], refetched=[])
    package = drop_case.render_input(case)
    assert "ACME" in package and "18.9" in package


# --- submitting an investigation --------------------------------------------

def _reply(**over) -> dict:
    base = {"what_happened": "An unrelated FDA clearance re-rated the name.",
            "visible_evidence": ["market cap below floor"],
            "miss_category": "unforeseeable",
            "notes": "", "evidence_urls": [], "confidence": 0.6}
    base.update(over)
    return base


def test_submit_merges_the_investigation_into_the_measurement():
    case = drop_case.open_case(_result(), record_at_decision=[], refetched=[])
    review = drop_case.submit(case, _reply(miss_category="gate_correct"))
    assert review.ticker == "ACME"
    assert review.z == 2.0                      # measurement untouched
    assert review.miss_category is MissCategory.GATE_CORRECT
    assert review.what_happened.startswith("An unrelated")


def test_unforeseeable_is_overturned_when_the_news_was_public_in_time():
    """The load-bearing rule (§5.2(3), 2026-08-01 revision).

    "Unforeseeable" is the one category that closes an inquiry — nothing to
    fix, nothing to change. If the piece of news that moved the stock was
    published before we decided and simply was not in our record, that is a
    defect in how we collect, which is the most fixable kind there is. Code
    enforces it because the easy answer must not be the unfalsifiable one
    (invariant 3).
    """
    case = drop_case.open_case(
        _result(),
        record_at_decision=[],
        refetched=[_news("new FDA clearance", date(2026, 6, 30))])
    review = drop_case.submit(case, _reply(miss_category="unforeseeable"))
    assert review.miss_category is MissCategory.COLLECTION_GAP
    assert "collection_gap" in review.notes  # the override is recorded, not silent


def test_unforeseeable_stands_when_nothing_was_publicly_available():
    case = drop_case.open_case(_result(), record_at_decision=[], refetched=[])
    review = drop_case.submit(case, _reply(miss_category="unforeseeable"))
    assert review.miss_category is MissCategory.UNFORESEEABLE


def test_other_verdicts_are_not_touched_by_the_override():
    case = drop_case.open_case(
        _result(), record_at_decision=[],
        refetched=[_news("new FDA clearance", date(2026, 6, 30))])
    review = drop_case.submit(case, _reply(miss_category="gate_correct"))
    assert review.miss_category is MissCategory.GATE_CORRECT


def test_confidence_is_clamped_not_trusted():
    case = drop_case.open_case(_result(), record_at_decision=[], refetched=[])
    review = drop_case.submit(case, _reply(confidence=3.5))
    assert review.confidence == 1.0


def test_an_unknown_category_is_refused():
    """It is a join key: a value nobody counts is worse than an error."""
    case = drop_case.open_case(_result(), record_at_decision=[], refetched=[])
    with pytest.raises(ValueError):
        drop_case.submit(case, _reply(miss_category="looked_cheap"))


def test_other_without_notes_is_refused():
    case = drop_case.open_case(_result(), record_at_decision=[], refetched=[])
    with pytest.raises(ValueError):
        drop_case.submit(case, _reply(miss_category="other", notes="  "))


# --- the queue on disk ------------------------------------------------------

def test_cases_round_trip_through_the_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("HAWKEYE_DROPS", str(tmp_path))
    case = drop_case.open_case(_result(), record_at_decision=[], refetched=[])
    drop_case.save_case(case)
    assert [c.id for c in drop_case.list_cases()] == [case.id]
    assert drop_case.load_case(case.id).measurement.ticker == "ACME"


def test_discard_removes_the_case_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HAWKEYE_DROPS", str(tmp_path))
    case = drop_case.open_case(_result(), record_at_decision=[], refetched=[])
    drop_case.save_case(case)
    assert drop_case.discard(case.id) is True
    assert drop_case.list_cases() == []


def test_a_corrupt_queue_file_warns_instead_of_vanishing(tmp_path, monkeypatch,
                                                         capsys):
    monkeypatch.setenv("HAWKEYE_DROPS", str(tmp_path))
    (tmp_path / "drc_broken.json").write_text("{ not json", encoding="utf-8")
    assert drop_case.list_cases() == []
    assert "drc_broken.json" in capsys.readouterr().err
