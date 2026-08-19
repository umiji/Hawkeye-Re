"""What the company guided, and what it was measured against (T-018).

The scan CSV is the only place the user can check the ranking against the
original numbers by hand. Every other score term already carried its inputs —
`EPS 実績/予想` sits beside `EPSサプライズ率` — while the guidance term
carried a single word, `開示あり`, and the figures it was computed from lived
only in the ledger. A guidance score could be doubted but not checked.

Three things are pinned here:

- **Six columns carry the figures**, per unit and per period, taken from the
  reading the ranking actually made rather than re-derived when the file is
  written. A print row can gain a guidance reading afterwards; showing the
  new figures under the old score would be worse than showing none.
- **The error column is a sentence.** It held three words — 数値 / 見通し /
  発表文 — and on 2026-08-19 the user asked what 数値 meant, which is the
  whole case for this change.
- **The comparison caveat stops asserting a cause it does not know.**
  `売上レンジの中央値で比較(EPSレンジの開示が無いため)` was printed even when
  the company HAD published an EPS range: AS guided $0.31–0.33 on 2026-08-19
  and the report said in one line that it compared on EPS and in the next
  that no EPS range existed.
"""
from __future__ import annotations

import csv
import io
from datetime import date

import pytest

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import (
    GuidanceState,
    ScreenedCandidate,
    ScreenedCandidateStage,
)
from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    EarningsPrint,
    GuidanceReading,
    PrintSource,
    SnapshotKind,
)
from hawkeye.reports.quality_ja import render_leg_ja
from hawkeye.reports.scan_report_ja import scan_report_csv
from hawkeye.scout.quality import assess_earnings, guidance_comparisons
from hawkeye.scout.scout import ScoutCandidate, _measured

CONFIG = HawkeyeConfig()


# --- material ---------------------------------------------------------------
#
# AS's real shape from the 2026-08-19 scan: an EPS range and a sales range for
# the next quarter, and a full-year EPS range the company had just raised.

def a_print(readings=(), **overrides) -> EarningsPrint:
    base = dict(stock_id="cik:0001988894", ticker="AS",
                fiscal_quarter="2026-Q2", report_date=date(2026, 8, 18),
                source=PrintSource.WHISPERS, eps_actual=0.22,
                eps_actual_rows=[0.22], revenue_actual=1.63e9,
                guidance_readings=list(readings))
    base.update(overrides)
    return EarningsPrint(**base)


def a_consensus(**overrides) -> ConsensusSnapshot:
    base = dict(stock_id="cik:0001988894", ticker="AS",
                fiscal_quarter="2026-Q2", kind=SnapshotKind.PRE_REGISTERED,
                eps_avg=0.11, eps_calendar=0.11, revenue_avg=1.55e9,
                next_quarter_eps_avg=0.39, next_quarter_revenue_avg=2.06e9,
                full_year_eps_avg=1.23, full_year_revenue_avg=8.07e9,
                full_year_period="FY2026")
    base.update(overrides)
    return ConsensusSnapshot(**base)


QUARTER = GuidanceReading(period="2026-Q3", eps_low=0.31, eps_high=0.33,
                          revenue_low=2.07e9, revenue_high=2.11e9,
                          source_excerpt="third quarter earnings of $0.31 to "
                                         "$0.33 per share")
YEAR = GuidanceReading(period="FY2026", eps_low=1.27, eps_high=1.30,
                       source_excerpt="2026 earnings of $1.27 to $1.30 "
                                      "per share")


def comparisons_for(readings, consensus=None):
    quality = assess_earnings(a_print(readings),
                              a_consensus() if consensus is None else consensus,
                              CONFIG)
    return guidance_comparisons(quality)


def a_candidate(ticker="AS", **overrides) -> ScreenedCandidate:
    base = dict(scan_id=3, ticker=ticker, event_date=date(2026, 8, 18),
                eps_surprise_pct=100.0, revenue_surprise_pct=5.3,
                stage=ScreenedCandidateStage.RANKING_CUTOFF, rank=1,
                score=75.66, score_version="full")
    base.update(overrides)
    return ScreenedCandidate(**base)


def cells_for(candidate: ScreenedCandidate) -> dict:
    header, row = list(csv.reader(io.StringIO(
        scan_report_csv([candidate]))))[:2]
    return dict(zip(header, row))


# --- what the ranking read, kept for the file -------------------------------

def test_each_period_and_each_unit_the_company_guided_is_recorded():
    """AS guided EPS and sales for the quarter and EPS for the year: three
    statements, and the record used to keep the percentage of one of them."""
    out = comparisons_for([QUARTER, YEAR])

    assert [(c.period, c.unit) for c in out] == [
        ("2026-Q3", "eps"), ("2026-Q3", "revenue"), ("FY2026", "eps")]


def test_the_two_figures_are_the_ones_the_comparison_divided():
    """The midpoint of the company's range over the consensus for that period
    — not the range ends, and not the consensus for a different period."""
    quarter_eps, quarter_revenue, year_eps = comparisons_for([QUARTER, YEAR])

    assert (quarter_eps.company, quarter_eps.consensus) == (0.32, 0.39)
    assert (quarter_revenue.company, quarter_revenue.consensus) == (2.09e9,
                                                                    2.06e9)
    assert (year_eps.company, year_eps.consensus) == (pytest.approx(1.285),
                                                     1.23)


def test_the_period_carries_which_yardstick_it_belongs_to():
    """`2026-Q3` alone does not say whether that quarter is the one ahead of
    the print. Nothing downstream keeps a fiscal quarter to work it out."""
    quarter_eps, _, year_eps = comparisons_for([QUARTER, YEAR])

    assert quarter_eps.period_kind == "next_quarter"
    assert year_eps.period_kind == "full_year"


def test_a_period_neither_yardstick_covers_says_so():
    """A company guiding two quarters out is not compared with anything. The
    figure is still recorded — it is what the company said."""
    far = GuidanceReading(period="2026-Q4", eps_low=0.40, eps_high=0.42)

    only, = comparisons_for([far])

    assert (only.period_kind, only.consensus) == ("other", None)
    assert only.company == pytest.approx(0.41)


def test_a_unit_with_no_yardstick_keeps_the_company_figure():
    """The company guided this and we held nothing to measure it with — that
    is the answer to why a guidance leg scored nothing, and dropping the entry
    would leave the question unanswerable from the file."""
    out = comparisons_for([QUARTER],
                          a_consensus(next_quarter_revenue_avg=None))

    assert [(c.unit, c.consensus) for c in out] == [("eps", 0.39),
                                                    ("revenue", None)]


def test_a_range_the_company_fenced_records_no_yardstick():
    """A conditioned range is not measured against a consensus set without
    the condition (ACA's barge business). Printing the consensus beside it
    would invite exactly the comparison the gate refuses."""
    fenced = GuidanceReading(period="2026-Q3", eps_low=0.31, eps_high=0.33,
                             qualifier="excluding its barge business")

    only, = comparisons_for([fenced])

    assert (only.company, only.consensus) == (0.32, None)


def test_a_company_that_guided_nothing_records_nothing():
    assert comparisons_for([]) == []


def test_the_scan_record_carries_the_figures_rather_than_re_deriving_them():
    """The wiring. A print row can gain a guidance reading after the scan, so
    the file has to show what the RANKING read, not what the ledger now
    holds."""
    quality = assess_earnings(a_print([QUARTER, YEAR]), a_consensus(), CONFIG)
    candidate = ScoutCandidate(ticker="AS", event_date=date(2026, 8, 18),
                               eps_surprise_pct=100.0,
                               revenue_surprise_pct=5.3, quality=quality)

    recorded = _measured(candidate)["guidance_comparisons"]

    assert [(c.period, c.unit) for c in recorded] == [
        ("2026-Q3", "eps"), ("2026-Q3", "revenue"), ("FY2026", "eps")]


def test_a_row_written_before_these_columns_existed_still_loads():
    """Invariant 1. An older row has no such key and reads back as "nothing
    recorded", which is what the file prints for it."""
    older = ScreenedCandidate.model_validate_json(
        a_candidate().model_dump_json(exclude={"guidance_comparisons"}))

    assert older.guidance_comparisons == []


# --- the six columns --------------------------------------------------------

def test_the_six_columns_carry_both_periods_aligned_entry_for_entry():
    """Two periods go DOWN the row, not across it. The three cells of a unit
    stay aligned so the reader can pair a period with its own two figures."""
    cells = cells_for(a_candidate(
        guidance_state=GuidanceState.DISCLOSED,
        guidance_comparisons=comparisons_for([QUARTER, YEAR])))

    assert cells["EPSガイダンス 対象期"] == "2026-Q3（翌四半期）、FY2026（通期）"
    assert cells["EPSガイダンス 会社見通し"] == "0.32、1.285"
    assert cells["EPSガイダンス コンセンサス"] == "0.39、1.23"
    assert cells["売上ガイダンス 対象期"] == "2026-Q3（翌四半期）"
    assert cells["売上ガイダンス 会社見通し"] == "2090000000"
    assert cells["売上ガイダンス コンセンサス"] == "2060000000"


def test_a_company_that_guided_only_sales_leaves_the_eps_columns_empty():
    """KLAR guided revenue and no EPS range. Filling the EPS cells with a
    dash would read as "we tried and failed" rather than "it said nothing"."""
    sales_only = GuidanceReading(period="2026-Q3", revenue_low=2.07e9,
                                 revenue_high=2.11e9)

    cells = cells_for(a_candidate(
        guidance_state=GuidanceState.DISCLOSED,
        guidance_comparisons=comparisons_for([sales_only])))

    assert cells["EPSガイダンス 対象期"] == ""
    assert cells["EPSガイダンス 会社見通し"] == ""
    assert cells["EPSガイダンス コンセンサス"] == ""
    assert cells["売上ガイダンス 会社見通し"] == "2090000000"


def test_a_company_that_guided_nothing_leaves_all_six_empty():
    """Empty, never `-`: the ガイダンス column beside them already says
    開示なし in words, and two spellings of "nothing" in one row is one
    spelling too many."""
    cells = cells_for(a_candidate(guidance_state=GuidanceState.NOT_PUBLISHED))

    assert [cells[h] for h in cells if "ガイダンス " in h] == [""] * 6
    assert cells["ガイダンス"] == "開示なし"


def test_a_missing_yardstick_shows_as_a_dash_beside_the_figure_it_lacks():
    """The period and the company's figure stay; only the bar is missing, and
    the cell says which one it was."""
    cells = cells_for(a_candidate(
        guidance_state=GuidanceState.DISCLOSED,
        guidance_comparisons=comparisons_for(
            [QUARTER], a_consensus(next_quarter_revenue_avg=None))))

    assert cells["売上ガイダンス 対象期"] == "2026-Q3（翌四半期）"
    assert cells["売上ガイダンス 会社見通し"] == "2090000000"
    assert cells["売上ガイダンス コンセンサス"] == "-"


def test_the_existing_columns_are_untouched():
    """Added, not rearranged: a reader with last week's file open should find
    every column they had, in the order they had it."""
    header = list(csv.reader(io.StringIO(scan_report_csv([a_candidate()]))))[0]
    after = header[header.index("エラー"):]

    assert header[:6] == ["ティッカー", "発表日", "順位", "到達段階",
                          "数値の出所", "EPS 実績/予想"]
    assert after[:3] == ["エラー", "点数", "EPSで得た点"]


# --- the error column -------------------------------------------------------

def test_each_failure_is_written_out_as_a_sentence():
    """`数値` told nobody anything. The file is opened without the report's
    §④ beside it to explain the word."""
    numbers = cells_for(a_candidate(
        numbers_reason="whispers_eps_incomplete"))["エラー"]
    outlook = cells_for(a_candidate(
        guidance_state=GuidanceState.UNREADABLE,
        guidance_reason="no_summary_from_feed"))["エラー"]
    release = cells_for(a_candidate(cause_blocks_refused=2))["エラー"]

    assert numbers == ("決算専門サイトからEPSまたは売上の数値が取れず、"
                       "決算カレンダーの数字で判定しました。")
    assert outlook == "会社は見通しを述べているのに、こちらが読み取れませんでした。"
    assert release == ("審理に渡す決算発表文の抜粋の一部が、却下されたか"
                       "語句を改変されていました。")


def test_two_failures_read_as_two_sentences():
    """Joined by their own full stops. A separator would leave the first
    sentence ending in `しました／`, which reads as a fragment."""
    cell = cells_for(a_candidate(
        numbers_reason="whispers_eps_incomplete",
        cause_blocks_altered=1))["エラー"]

    assert cell.count("。") == 2
    assert cell.startswith("決算専門サイトから")
    assert cell.endswith("語句を改変されていました。")


def test_a_row_with_nothing_wrong_leaves_the_column_empty():
    assert cells_for(a_candidate())["エラー"] == ""


# --- the comparison caveat --------------------------------------------------

def test_comparing_on_both_never_claims_the_eps_range_was_missing():
    """The AS line, verbatim from the 2026-08-19 scan:

        - EPSレンジの中央値で比較
        - 売上レンジの中央値で比較(EPSレンジの開示が無いため)

    AS discloses $0.31–0.33. The second line contradicts the first.
    """
    quality = assess_earnings(a_print([QUARTER]), a_consensus(), CONFIG)

    out = render_leg_ja(quality.guidance)

    assert "EPSレンジの中央値で比較" in out
    assert "売上レンジの中央値でも比較" in out
    assert "開示が無いため" not in out
    assert "比較できなかったため" not in out


def test_comparing_on_sales_alone_still_says_why():
    """Deleting the caveat outright would pass the test above and lose the
    reason a sales-only comparison happened. The reason is stated without
    naming a cause the flag does not establish — the company may have
    published no EPS range, or there may have been no EPS consensus.
    """
    sales_only = GuidanceReading(period="2026-Q3", revenue_low=2.07e9,
                                 revenue_high=2.11e9)
    quality = assess_earnings(a_print([sales_only]), a_consensus(), CONFIG)

    out = render_leg_ja(quality.guidance)

    assert "売上レンジの中央値で比較(EPSでは比較できなかったため)" in out
    assert "EPSレンジの中央値で比較(" not in out
