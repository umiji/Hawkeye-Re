"""Timestamps are stamped, stored and shown in JST (2026-07-31).

The change is about legibility, so these tests assert on what is actually
observable: the offset that ends up in the ledger, the order rows come back
in once pre-change UTC rows sit next to post-change JST rows, the rendered
string, and — the part that must NOT move — the calendar date every
forward-return measurement is anchored on.
"""
from datetime import date, datetime, timedelta, timezone

from hawkeye.contracts.models import (
    JST,
    DecisionType,
    GateReport,
    Recommendation,
    RecommendationStatus,
    ScreenedCandidate,
    ScreenedCandidateStage,
    Verdict,
    now,
    utc_date,
)
from hawkeye.ledger.store import Ledger
from hawkeye.reports.render_ja import fmt_jst, render_recommendation_ja
from hawkeye.scout.drop_review import from_recommendation, from_screened
from hawkeye.tribunal.pipeline import parse_thesis
from tests.conftest import make_brief, thesis_payload
from tests.test_drop_review_store import make_review

# Two instants that a text sort gets backwards: the JST one is EARLIER in
# real time, but its date part reads later. This is the exact pair that
# `ORDER BY <timestamp column>` mis-sorts once both offsets exist in one
# table, which is what the mixed-offset tests below pin down.
EARLIER_JST = datetime(2026, 8, 1, 0, 30, tzinfo=JST)          # = 07-31 15:30Z
LATER_UTC = datetime(2026, 7, 31, 23, 0, tzinfo=timezone.utc)


def make_rec(ticker: str = "TEST", **overrides) -> Recommendation:
    base = dict(
        ticker=ticker,
        brief=make_brief(),
        gate_report=GateReport(),
        thesis=parse_thesis(thesis_payload()),
        verdict=Verdict(decision=DecisionType.BUY, conviction=0.6,
                        rationale="test"),
    )
    base.update(overrides)
    return Recommendation(**base)


def make_screened(ticker: str, recorded_at: datetime) -> ScreenedCandidate:
    return ScreenedCandidate(
        recorded_at=recorded_at, scan_id=1, ticker=ticker,
        event_date=date(2026, 7, 29), eps_surprise_pct=8.0, score=1.0,
        score_version="full", stage=ScreenedCandidateStage.GATE_REJECT)


def test_the_text_order_of_the_two_offsets_really_is_backwards():
    """Guards the premise of every mixed-offset test below: if this ever
    stops holding, those tests would pass for the wrong reason."""
    assert EARLIER_JST < LATER_UTC
    assert LATER_UTC.isoformat() < EARLIER_JST.isoformat()


# --- what gets written -----------------------------------------------------

def test_new_records_are_stamped_in_jst(tmp_path):
    ledger = Ledger(str(tmp_path / "t.db"))
    assert now().utcoffset() == timedelta(hours=9)

    rec = make_rec()
    assert rec.created_at.utcoffset() == timedelta(hours=9)
    ledger.record_recommendation(rec, RecommendationStatus.PROPOSED)

    stored = ledger.list()[0]["created_at"]
    assert datetime.fromisoformat(stored).utcoffset() == timedelta(hours=9)
    event_ts = ledger.events(rec.id)[0]["ts"]
    assert datetime.fromisoformat(event_ts).utcoffset() == timedelta(hours=9)
    scan_id = ledger.record_scan({}, 1, 1, 1, 1, ["TEST"])
    assert datetime.fromisoformat(
        ledger.list_scans()[0]["ts"]).utcoffset() == timedelta(hours=9)
    assert ledger.last_scan_at().utcoffset() == timedelta(hours=9)
    assert scan_id == 1


# --- reading it back in the right order ------------------------------------

def test_recommendations_come_back_chronologically_across_the_switch(tmp_path):
    ledger = Ledger(str(tmp_path / "t.db"))
    late = make_rec("LATE", created_at=LATER_UTC)
    early = make_rec("EARLY", created_at=EARLIER_JST)
    ledger.record_recommendation(late, RecommendationStatus.PROPOSED)
    ledger.record_recommendation(early, RecommendationStatus.PROPOSED)

    assert [r["ticker"] for r in ledger.list()] == ["EARLY", "LATE"]

    for rec in (late, early):
        ledger.record_decision(rec.id, approved=True)
        ledger.record_entry(rec.id, price=10.0, shares=1,
                            trade_date=date(2026, 8, 3))
    assert [r.ticker for r in ledger.open_positions()] == ["EARLY", "LATE"]
    assert ledger.verify_chain()


def test_screened_candidates_come_back_chronologically(tmp_path):
    ledger = Ledger(str(tmp_path / "t.db"))
    ledger.record_screened_candidates(
        1, [make_screened("LATE", LATER_UTC),
            make_screened("EARLY", EARLIER_JST)])
    assert [c.ticker for c in ledger.screened_candidates()] == ["EARLY", "LATE"]
    assert ledger.verify_chain()


def test_drop_reviews_come_back_chronologically(tmp_path):
    ledger = Ledger(str(tmp_path / "t.db"))
    ledger.record_drop_reviews([
        make_review(ticker="LATE", screened_candidate_id="scr_late",
                    reviewed_at=LATER_UTC),
        make_review(ticker="EARLY", screened_candidate_id="scr_early",
                    reviewed_at=EARLIER_JST)])
    assert [r.ticker for r in ledger.drop_reviews()] == ["EARLY", "LATE"]
    assert ledger.verify_chain()


# --- what the user sees ----------------------------------------------------

def test_fmt_jst_converts_and_labels():
    assert fmt_jst(datetime(2026, 7, 31, 14, 45, tzinfo=timezone.utc)) \
        == "2026-07-31 23:45 JST"
    assert fmt_jst("2026-07-31T14:45:00+00:00") == "2026-07-31 23:45 JST"
    assert fmt_jst(EARLIER_JST) == "2026-08-01 00:30 JST"
    # A hand-edited/unreadable value is shown as-is rather than crashing a
    # listing — losing the whole table over one bad cell helps nobody.
    assert fmt_jst("not-a-timestamp") == "not-a-timestamp"


def test_the_report_header_says_jst_not_utc():
    rendered = render_recommendation_ja(
        make_rec(created_at=datetime(2026, 7, 31, 14, 45, tzinfo=timezone.utc)))
    assert "2026-07-31 23:45 JST" in rendered
    assert "UTC" not in rendered


# --- what must NOT move ----------------------------------------------------

def test_measurement_anchor_stays_on_the_utc_calendar_date():
    """Forward returns are measured from the day a decision was made. Moving
    the display timezone must not silently shift that day — a JST-dated
    anchor would start the holding period one trading day later for any run
    after 15:00 UTC, changing every past-vs-future comparison."""
    assert utc_date(EARLIER_JST) == date(2026, 7, 31)
    assert utc_date(LATER_UTC) == date(2026, 7, 31)

    rec = make_rec(created_at=EARLIER_JST)
    assert from_recommendation(rec).decision_date == date(2026, 7, 31)
    candidate = make_screened("SCR", EARLIER_JST)
    assert from_screened(candidate).decision_date == date(2026, 7, 31)
