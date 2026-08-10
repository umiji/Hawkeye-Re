"""The inspection table: the check sheet the user reads before the ranking.

Its job is to make a retrieval fault visible. So these tests are mostly about
what must NOT be silently absent: a name the feed declined, a calendar that
contradicted itself, a quarter with no consensus, a guidance comparison that
was refused, and who read the guidance sentence.
"""
from __future__ import annotations

import csv
import io
from datetime import date

import pytest

from hawkeye.reports.monitor_ja import inspection_csv, render_inspection_ja
from hawkeye.scout.earnings import EarningsEvent
from hawkeye.scout.inspection import build_inspection, was_asked
from hawkeye.scout.quality import (
    EarningsQuality,
    LegStatus,
    LegVerdict,
    QuarterVerdict,
)
from hawkeye.scout.scout import ScoutCandidate, ScoutResult


def an_event(ticker="TEST", **overrides) -> EarningsEvent:
    base = dict(ticker=ticker, day=date(2026, 8, 5),
                eps_actual=1.20, eps_estimate=1.00,
                revenue_actual=1.05e9, revenue_estimate=1.00e9,
                fiscal_quarter="2026-Q2", numbers_source="whispers")
    base.update(overrides)
    return EarningsEvent(**base)


def a_quality(ticker="TEST", guidance=None, **overrides) -> EarningsQuality:
    base = dict(
        ticker=ticker, fiscal_quarter="2026-Q2",
        eps=LegVerdict(leg="eps", status=LegStatus.BEAT, surprise_pct=20.0),
        revenue=LegVerdict(leg="revenue", status=LegStatus.BEAT,
                           surprise_pct=5.0),
        guidance=guidance or LegVerdict(leg="guidance",
                                        status=LegStatus.ABSENT,
                                        flags=("no_guidance_in_source",)),
        verdict=QuarterVerdict.GOOD_QUARTER, score=42.0)
    base.update(overrides)
    return EarningsQuality(**base)


def a_result(passed=(), rejected=(), capped=(), held=()) -> ScoutResult:
    return ScoutResult(scan_start=date(2026, 8, 1), scan_end=date(2026, 8, 7),
                       scanned=0, screened=0, enriched=0,
                       passed=list(passed), rejected=list(rejected),
                       capped=list(capped), held=list(held))


def a_candidate(ticker="TEST", quality=None, **overrides) -> ScoutCandidate:
    base = dict(ticker=ticker, event_date=date(2026, 8, 5),
                eps_surprise_pct=20.0, revenue_surprise_pct=5.0,
                quality=quality)
    base.update(overrides)
    return ScoutCandidate(**base)


# --- who is on the sheet ---------------------------------------------------

def test_a_name_the_feed_answered_for_is_on_the_sheet():
    assert was_asked(an_event(numbers_source="whispers")) is True


def test_a_name_the_feed_declined_is_on_the_sheet():
    """Declining is an answer. Leaving it off would make the sheet look clean
    exactly when the retrieval went wrong."""
    event = an_event(numbers_source="calendar",
                     numbers_reason="whispers_previous_quarter")
    assert was_asked(event) is True


def test_a_name_nobody_asked_about_is_not_a_row_but_is_counted():
    never_asked = an_event("QUIET", numbers_source="calendar")
    assert was_asked(never_asked) is False
    inspection = build_inspection([never_asked], a_result())
    assert inspection.rows == []
    assert inspection.counts.calendar_only_not_asked == 1
    assert inspection.counts.asked == 0


def test_the_sheet_is_independent_of_whether_a_name_was_ranked():
    """§10: 全件出す. A name that failed the gates is still a name whose data
    has to be checkable."""
    events = [an_event("AAA"), an_event("BBB"), an_event("CCC")]
    result = a_result(passed=[a_candidate("AAA", a_quality("AAA"))],
                      rejected=[a_candidate("BBB", a_quality("BBB"))])
    rows = build_inspection(events, result).rows
    assert {r.ticker for r in rows} == {"AAA", "BBB", "CCC"}
    stages = {r.ticker: r.stage for r in rows}
    assert stages["AAA"] == "gate_passed"
    assert stages["BBB"] == "gate_reject"
    # Asked about, but the surprise never cleared the screen, so no candidate
    # was ever built for it. That is a different fact from failing the gates.
    assert stages["CCC"] == "below_screen"


# --- the frequencies §10 requires -----------------------------------------

def test_every_required_frequency_is_counted():
    events = [
        an_event("AAA"),
        an_event("BBB", numbers_source="calendar",
                 numbers_reason="whispers_unreachable"),
        an_event("CCC", conflicting_estimates=True),
        an_event("DDD", eps_estimate=None),
    ]
    qualified = LegVerdict(leg="guidance", status=LegStatus.ABSENT,
                           flags=("guidance_scope_qualified",),
                           excerpt="excluding its barge business")
    result = a_result(passed=[a_candidate("AAA", a_quality("AAA", qualified))])
    counts = build_inspection(events, result, revisions_seen=2).counts
    assert counts.asked == 4
    assert counts.answered_by_feed == 3
    assert counts.fell_back_to_calendar == 1
    assert counts.conflicting_calendar_rows == 1
    assert counts.no_consensus_this_quarter == 1
    assert counts.guidance_declined_scope_qualified == 1
    assert counts.revisions_seen == 2


def test_the_frequencies_are_printed_even_when_all_zero():
    """A line that vanishes at zero reads as a check nobody ran."""
    page = render_inspection_ja(build_inspection([an_event()], a_result()))
    assert "矛盾する行を返した: **0件**" in page
    assert "比較を見送った: **0件**" in page
    assert "書き換えていた: **0件**" in page


def test_an_empty_sheet_says_so_rather_than_printing_nothing():
    page = render_inspection_ja(build_inspection([], a_result()))
    assert "該当なし" in page


# --- what the reader has to be able to see --------------------------------

def test_the_reason_the_feeds_numbers_were_not_used_is_in_japanese():
    event = an_event("BBB", numbers_source="calendar",
                     numbers_reason="whispers_previous_quarter")
    page = render_inspection_ja(build_inspection([event], a_result()))
    assert "whispers_previous_quarter" not in page, (
        "a bare identifier is a reason the reader cannot act on")
    assert "決算専門サイトの数字を使いませんでした" in page


def test_the_companys_own_words_are_quoted_when_a_comparison_was_refused():
    qualified = LegVerdict(leg="guidance", status=LegStatus.ABSENT,
                           flags=("guidance_scope_qualified",),
                           excerpt="excluding its barge business")
    result = a_result(passed=[a_candidate("ACA", a_quality("ACA", qualified))])
    page = render_inspection_ja(
        build_inspection([an_event("ACA")], result))
    assert "excluding its barge business" in page


def test_who_read_the_guidance_and_with_which_model_is_on_the_sheet():
    """Task 9's third bullet. Two runs' guidance readings are only comparable
    if the reader is recorded, so the column cannot be optional."""
    quality = a_quality("ALGT", guidance_extractor="agent",
                        guidance_extractor_model="claude-opus-4-8")
    result = a_result(passed=[a_candidate("ALGT", quality)])
    inspection = build_inspection([an_event("ALGT")], result)
    assert inspection.rows[0].guidance_extractor == "agent"
    assert inspection.rows[0].guidance_extractor_model == "claude-opus-4-8"
    # Summarised on one line rather than repeated per row: every row usually
    # has the same reader, and 21 identical notes hid the failures that mattered
    # on the first live run. A run that CHANGED model mid-way therefore shows
    # two entries here, which is the case worth spotting.
    page = render_inspection_ja(inspection)
    assert "ガイダンスを読んだ読み手: AI(claude-opus-4-8) 1件" in page


def test_two_readers_in_one_scan_are_both_named():
    """Readings from different models are not comparable, so a mixed scan has
    to be visible rather than averaged into 「AI」."""
    result = a_result(passed=[
        a_candidate("AAA", a_quality("AAA", guidance_extractor="agent",
                                     guidance_extractor_model="claude-opus-4-8")),
        a_candidate("BBB", a_quality("BBB", guidance_extractor="agent",
                                     guidance_extractor_model="claude-sonnet-5"))])
    page = render_inspection_ja(
        build_inspection([an_event("AAA"), an_event("BBB")], result))
    assert "AI(claude-opus-4-8) 1件" in page
    assert "AI(claude-sonnet-5) 1件" in page


def test_an_unread_guidance_is_counted_not_listed_per_name():
    """Session mode stages every sentence for a later step, so this is the
    normal state of a scan — as a per-row note it buried everything else."""
    pending = LegVerdict(leg="guidance", status=LegStatus.ABSENT,
                         flags=("pending_extraction",))
    events = [an_event(f"T{i}") for i in range(3)]
    result = a_result(passed=[a_candidate(f"T{i}", a_quality(f"T{i}", pending))
                              for i in range(3)])
    inspection = build_inspection(events, result)
    assert inspection.counts.guidance_unread == 3
    page = render_inspection_ja(inspection)
    assert "ガイダンスが未読のまま: **3件**" in page
    assert "まだ読み取っていない" not in page, (
        "the routine state does not earn a line per name")
    # But the table cell still says which of the guidance reasons applies.
    assert "未読(AI待ち)" in page


def test_a_name_with_no_quarter_label_is_flagged_not_left_blank():
    page = render_inspection_ja(
        build_inspection([an_event("XXX", fiscal_quarter=None)], a_result()))
    assert "四半期ラベルを決められませんでした" in page


def test_the_sheet_says_it_is_not_a_decision_input():
    page = render_inspection_ja(build_inspection([an_event()], a_result()))
    assert "判断材料ではありません" in page


# --- CSV -------------------------------------------------------------------

def test_the_csv_has_one_line_per_row_on_the_screen():
    events = [an_event("AAA"), an_event("BBB"), an_event("CCC")]
    inspection = build_inspection(events, a_result())
    parsed = list(csv.reader(io.StringIO(inspection_csv(inspection))))
    assert len(parsed) == len(inspection.rows) + 1  # + the header
    assert parsed[0][0] == "ティッカー"


def test_the_csv_carries_the_columns_the_screen_cannot_fit():
    quality = a_quality("ALGT", guidance_extractor="agent",
                        guidance_extractor_model="claude-opus-4-8")
    inspection = build_inspection(
        [an_event("ALGT", eps_surprise_pct_reported=72.44)],
        a_result(passed=[a_candidate("ALGT", quality)]))
    rows = list(csv.DictReader(io.StringIO(inspection_csv(inspection))))
    assert rows[0]["EPSサプライズ率(提供元の公表値)"] == "72.44"
    assert rows[0]["ガイダンスの読み手のモデル"] == "claude-opus-4-8"
    assert rows[0]["カレンダーが矛盾行を返した"] == "いいえ"


def test_the_csv_translates_and_rounds_the_same_way_the_screen_does():
    """The file is read by the same person as the screen. It shipped with raw
    `gate_reject` / `whispers_server_error` and 502.7149321266968 in it."""
    inspection = build_inspection(
        [an_event("ATNM", numbers_source="calendar",
                  numbers_reason="whispers_server_error",
                  eps_actual=0.89, eps_estimate=-0.221)],
        a_result(rejected=[a_candidate("ATNM", a_quality("ATNM"))]))
    rows = list(csv.DictReader(io.StringIO(inspection_csv(inspection))))
    assert rows[0]["到達段階"] == "入口ゲートで落選"
    assert rows[0]["数値の出所"] == "決算カレンダー"
    assert "決算専門サイトがエラーを返しました" in \
        rows[0]["決算専門サイトを使わなかった理由"]
    assert rows[0]["EPSサプライズ率(自前計算)"] == "502.71"


def test_the_csv_renders_a_missing_number_as_empty_not_as_the_word_none():
    inspection = build_inspection([an_event(eps_estimate=None)], a_result())
    rows = list(csv.DictReader(io.StringIO(inspection_csv(inspection))))
    assert rows[0]["EPS予想"] == ""


# --- where it appears in the report ---------------------------------------

def test_the_sheet_comes_before_the_ranking():
    """§10: 順位確定より前に出す. A reader who has already read the shortlist
    does not go back and check the data it was built from."""
    from hawkeye.reports.render_ja import render_scout_ja
    result = a_result(passed=[a_candidate("AAA", a_quality("AAA"))])
    result.inspection = build_inspection([an_event("AAA")], result)
    page = render_scout_ja(result)
    assert "取得データ点検表" in page
    assert page.index("取得データ点検表") < page.index("AAA"), (
        "the check sheet has to precede the first mention of a ranked name")


@pytest.mark.parametrize("reason", [
    "whispers_no_record", "whispers_previous_quarter", "whispers_later_print",
    "whispers_announcement_time_missing", "whispers_eps_incomplete",
    "whispers_revenue_incomplete", "whispers_unreachable",
    "whispers_server_error",
])
def test_every_reason_the_feed_can_decline_for_has_a_japanese_gloss(reason):
    """The full set from hawkeye/scout/numbers.py. These reached the reader as
    bare identifiers until 2026-08-10 — the counts were translated, the
    per-name reason was not."""
    from hawkeye.reports.quality_ja import _FLAG
    assert reason in _FLAG
