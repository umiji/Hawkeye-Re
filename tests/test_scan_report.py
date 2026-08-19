"""The scan report the USER reads (task 9).

The check sheet built earlier prints into `hawkeye scout`'s output, which the
orchestrating session reads and the user never sees. This report is the other
half: one document, handed to the user inside `/hawkeye-run`, that says which
three names are about to be argued over, what earned them their score, and
what happened to everything else the scan looked at.

Every test here pins something the user asked for by name, so a later
refactor cannot quietly drop a section and still pass.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone

import pytest

from hawkeye.contracts.models import (
    AnalystTrend,
    GateReport,
    GateResult,
    GuidanceState,
    InsiderActivity,
    NewsItem,
    ScoreBreakdown,
    ScreenedCandidate,
    ScreenedCandidateStage,
)
from hawkeye.reports.scan_report_ja import render_scan_report_ja, scan_report_csv
from hawkeye.scout.earnings import score_candidate, score_parts

SCAN = {"id": 12, "ts": "2026-08-10T21:00:00+00:00",
        "params": {"window_start": "2026-08-07", "window_end": "2026-08-10",
                   "min_eps_surprise": 5.0},
        "scanned": 1395, "screened": 379, "enriched": 45, "gate_passed": 15,
        "tickers": ["AAA", "BBB", "CCC"]}


def make_candidate(ticker: str = "AAA", *, rank=None,
                   stage=ScreenedCandidateStage.RANKING_CUTOFF,
                   **overrides) -> ScreenedCandidate:
    base = dict(
        scan_id=12, ticker=ticker, event_date=date(2026, 8, 7),
        eps_surprise_pct=12.5, revenue_surprise_pct=3.0,
        numbers_source="whispers", score=41.5, score_version="full",
        price=48.0, stage=stage, rank=rank)
    base.update(overrides)
    return ScreenedCandidate(**base)


def gate_report(*, passed: bool = True) -> GateReport:
    return GateReport(results=[
        GateResult(name="min_price", passed=True, hard=True,
                   value=48.0, threshold=5.0),
        GateResult(name="min_market_cap", passed=passed, hard=True,
                   value=3.1e8, threshold=5e8),
        GateResult(name="min_avg_dollar_volume", passed=True, hard=True,
                   value=6e7, threshold=5e6),
        GateResult(name="catalyst_freshness_days", passed=True, hard=True,
                   value=3.0, threshold=10.0),
        GateResult(name="event_gap_not_extreme", passed=True, hard=False,
                   value=8.0, threshold=25.0),
        GateResult(name="volatility_sane", passed=True, hard=False,
                   value=3.0, threshold=12.0),
        GateResult(name="earnings_proximity", passed=True, hard=False,
                   unverified=True, note="next earnings date unknown"),
    ])


# --- the score, split into what earned it -----------------------------------

@pytest.mark.parametrize("eps,revenue,gap", [
    (12.5, 3.0, 8.0),
    (None, None, None),
    (40.0, 60.0, -4.0),
    (2.0, None, 1.0),
    (5.0, 3.0, 30.0),
])
def test_score_parts_add_up_to_the_score_that_was_already_being_computed(
        eps, revenue, gap):
    """Splitting the score must not change it.

    The user is shown the breakdown, so the parts and the total have to be the
    same arithmetic — a breakdown that does not sum to the score on screen is
    worse than no breakdown at all.
    """
    parts = score_parts(eps, revenue, gap)
    assert round(sum(parts), 2) == score_candidate(eps, revenue, gap)


def test_quality_carries_the_breakdown_and_it_sums_to_the_score(config):
    from hawkeye.contracts.stocks import (
        ConsensusSnapshot, EarningsPrint, PrintSource,
    )
    from hawkeye.scout.quality import assess_earnings

    print_row = EarningsPrint(
        stock_id="stk_1", ticker="AAA", fiscal_quarter="2026-Q2",
        period_end=date(2026, 6, 30), report_date=date(2026, 8, 7),
        announced_at=datetime(
            2026, 8, 7, 20, 5, tzinfo=timezone.utc),
        source=PrintSource.WHISPERS, eps_actual=1.20, revenue_actual=1.05e9)
    consensus = ConsensusSnapshot(
        stock_id="stk_1", fiscal_quarter="2026-Q2",
        captured_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        eps_avg=1.00, revenue_avg=1.00e9)
    quality = assess_earnings(print_row, consensus, config,
                              gap_on_event_pct=8.0)
    assert quality.breakdown is not None
    assert round(quality.breakdown.total, 2) == pytest.approx(quality.score,
                                                              abs=0.01)


# --- ① the three names going to the tribunal --------------------------------

def test_the_top_three_are_named_with_their_score_breakdown():
    rows = [
        make_candidate("AAA", rank=1, gate_report=gate_report(),
                       score_breakdown=ScoreBreakdown(
                           eps=12.5, revenue=1.5, gap=15.0, guidance=5.0,
                           whisper=2.5)),
        make_candidate("BBB", rank=2, gate_report=gate_report()),
        make_candidate("CCC", rank=3, gate_report=gate_report()),
        make_candidate("DDD", rank=4, gate_report=gate_report()),
    ]
    out = render_scan_report_ja(SCAN, rows, top_n=3)
    head = out.split("走査した銘柄")[0]
    assert "AAA" in head and "BBB" in head and "CCC" in head
    assert "DDD" not in head
    # The breakdown, by the names the user used when asking for it.
    assert "EPS" in head and "売上" in head
    assert "囁き予想" in head and "決算当日の値動き" in head


def test_a_candidate_recorded_before_the_breakdown_existed_says_so():
    """Old ledger rows have no breakdown. Printing zeros would be a lie about
    what earned the score; saying the figure was never recorded is not."""
    rows = [make_candidate("AAA", rank=1, gate_report=gate_report())]
    out = render_scan_report_ja(SCAN, rows, top_n=3)
    assert "内訳は記録されていません" in out
    assert "41.5" in out          # the score itself is still shown


def test_a_run_that_forwarded_its_top_names_says_they_are_missing():
    """`--open-cases N` sends the top N straight on, so the drop table never
    records them. Printing the fourth name as if it were the first is exactly
    the silent substitution this report exists to prevent."""
    rows = [make_candidate("DDD", rank=4, gate_report=gate_report())]
    out = render_scan_report_ja(SCAN, rows, top_n=3)
    assert "詳細がここにありません" in out
    assert "AAA" in out and "BBB" in out and "CCC" in out   # named from the scan
    assert "--open-cases" in out


def test_the_seven_entry_gate_conditions_are_all_printed():
    rows = [make_candidate("AAA", rank=1, gate_report=gate_report())]
    out = render_scan_report_ja(SCAN, rows, top_n=1)
    for japanese in ("株価", "時価総額", "売買代金", "決算からの経過日数",
                     "決算当日の値動きの大きさ", "値動きの荒さ",
                     "次の決算までの日数"):
        assert japanese in out


# --- ② what was NOT the earnings figures ------------------------------------

def test_news_insider_and_analyst_are_listed_for_the_tribunal_names():
    rows = [make_candidate(
        "AAA", rank=1, gate_report=gate_report(),
        news=[NewsItem(headline="AAA wins a $200m defense contract",
                       source="Reuters",
                       published_at=datetime(2026, 8, 8, 12, 0,
                                             tzinfo=timezone.utc))],
        insider_activity=InsiderActivity(window_days=90, net_shares=-12000.0,
                                         buyers=1, sellers=4),
        analyst_trend=AnalystTrend(period=date(2026, 8, 1), strong_buy=4,
                                   buy=6, hold=2, sell=0, strong_sell=0,
                                   prior_period=date(2026, 7, 1),
                                   prior_strong_buy=2, prior_buy=5,
                                   prior_hold=4, prior_sell=1,
                                   prior_strong_sell=0))]
    out = render_scan_report_ja(SCAN, rows, top_n=1)
    assert "AAA wins a $200m defense contract" in out
    assert "2026-08-08" in out
    assert "Reuters" in out
    assert "インサイダー" in out
    assert "アナリスト" in out


def test_the_report_states_that_news_earned_no_points():
    """Agreed with the user on 2026-08-10: the score is EPS, revenue, guidance,
    the whisper and the event-day move. News is material handed to the
    tribunal, never a scoring input — and a section listing news beside a
    score invites exactly the opposite reading."""
    out = render_scan_report_ja(SCAN, [make_candidate("AAA", rank=1)], top_n=1)
    assert "点数には使っていません" in out


# --- ③ every name the scan looked at ----------------------------------------

def test_the_table_has_one_row_per_recorded_candidate():
    rows = [make_candidate(f"T{i}", rank=(i + 1 if i < 3 else None),
                           stage=(ScreenedCandidateStage.RANKING_CUTOFF
                                  if i < 3 else
                                  ScreenedCandidateStage.GATE_REJECT))
            for i in range(9)]
    out = render_scan_report_ja(SCAN, rows, top_n=3)
    table = out.split("走査した銘柄")[1]
    body = [line for line in table.splitlines()
            if line.startswith("| T")]
    assert len(body) == 9


def test_names_nobody_asked_about_are_counted_rather_than_listed():
    """A name that cleared the surprise screen and then sat below the request
    budget has calendar figures, a calendar source and the same one-line
    reason as every other such name. On the first live run there were 121 of
    them against 50 real rows, which is the burying the check sheet already
    got fixed for on 2026-08-10."""
    asked = [make_candidate("AAA", numbers_source="whispers")]
    untouched = [make_candidate(f"U{i}", numbers_source="calendar",
                                stage=ScreenedCandidateStage.ENRICHMENT_CAP)
                 for i in range(4)]
    out = render_scan_report_ja(SCAN, asked + untouched, top_n=0)
    table = out.split("走査した銘柄")[1]
    assert len([line for line in table.splitlines()
                if line.startswith("| ")]) == 3     # header + separator + AAA
    assert "ほかに **4件**" in out
    assert "CSVには全5件が入っています" in out


def test_the_csv_still_holds_every_recorded_name():
    """The screen drops the rows nobody asked about; the file must not, or the
    complete record of a scan would exist nowhere the user can open."""
    rows = ([make_candidate("AAA")]
            + [make_candidate(f"U{i}", numbers_source="calendar",
                              stage=ScreenedCandidateStage.ENRICHMENT_CAP)
               for i in range(4)])
    parsed = list(csv.reader(io.StringIO(scan_report_csv(rows))))
    assert len(parsed) == 6                          # header + 5


def test_each_row_carries_the_source_and_why_it_was_dropped():
    rows = [make_candidate("AAA", stage=ScreenedCandidateStage.GATE_REJECT,
                           gate_report=gate_report(passed=False),
                           reject_reason="hard gate: min_market_cap"),
            make_candidate("BBB", stage=ScreenedCandidateStage.ENRICHMENT_CAP,
                           numbers_source="calendar",
                           numbers_reason="whispers_server_error")]
    out = render_scan_report_ja(SCAN, rows, top_n=0)
    assert "決算専門サイト" in out      # numbers_source == whispers, translated
    assert "決算カレンダー" in out      # numbers_source == calendar, translated
    assert "時価総額" in out            # the gate that actually refused AAA
    assert "gate_reject" not in out     # no raw English identifiers
    assert "enrichment_cap" not in out


# --- ④ what went wrong ------------------------------------------------------

def test_retrieval_failures_are_listed_by_ticker():
    rows = [make_candidate("AAA", numbers_source="calendar",
                           numbers_reason="whispers_server_error"),
            make_candidate("BBB", numbers_source="calendar",
                           numbers_reason="consensus_missing"),
            make_candidate("CCC")]
    out = render_scan_report_ja(SCAN, rows, top_n=0)
    errors = out.split("取得・読み取りに失敗したもの")[1]
    assert "AAA" in errors and "BBB" in errors
    assert "CCC" not in errors


def test_no_failures_still_prints_the_section():
    """A section that disappears at zero reads as a check that was not run."""
    out = render_scan_report_ja(SCAN, [make_candidate("AAA")], top_n=0)
    assert "取得・読み取りに失敗したもの" in out
    assert "0件" in out


# --- ⑤ the same rows, for a spreadsheet -------------------------------------

def test_csv_has_one_line_per_table_row_and_japanese_headers():
    rows = [make_candidate(f"T{i}") for i in range(6)]
    text = scan_report_csv(rows)
    parsed = list(csv.reader(io.StringIO(text)))
    assert len(parsed) == 7                       # header + 6
    assert parsed[0][0] == "ティッカー"
    assert "落選理由" in parsed[0]


def test_csv_translates_the_same_identifiers_the_screen_does():
    text = scan_report_csv([make_candidate(
        "AAA", stage=ScreenedCandidateStage.GATE_REJECT,
        numbers_source="calendar", numbers_reason="whispers_server_error")])
    assert "gate_reject" not in text
    assert "whispers_server_error" not in text


def test_empty_scan_reports_the_zero_rather_than_printing_nothing():
    out = render_scan_report_ja(SCAN, [], top_n=3)
    assert "0件" in out


# --- the ledger round trip the CLI actually performs -------------------------

def test_the_report_can_be_rebuilt_from_the_ledger_alone(tmp_path):
    """`hawkeye report scan` runs after the guidance step, not inside the scan,
    so everything it prints has to survive a write and a read of the ledger."""
    from hawkeye.ledger.store import Ledger

    ledger = Ledger(str(tmp_path / "test.db"))
    scan_id = ledger.record_scan(
        params={"window_start": "2026-08-07", "window_end": "2026-08-10"},
        scanned=1395, screened=379, enriched=45, gate_passed=2,
        tickers=["AAA", "BBB"])
    ledger.record_screened_candidates(scan_id, [
        make_candidate("AAA", rank=1, scan_id=scan_id,
                       gate_report=gate_report(),
                       score_breakdown=ScoreBreakdown(eps=12.5, revenue=1.5,
                                                      gap=15.0, guidance=5.0,
                                                      whisper=2.5)),
        make_candidate("BBB", rank=2, scan_id=scan_id,
                       gate_report=gate_report()),
    ])
    scan = ledger.scan()
    assert scan is not None and scan["id"] == scan_id
    rows = ledger.screened_candidates(scan_id=scan["id"])
    out = render_scan_report_ja(scan, rows, top_n=2)
    assert "AAA" in out and "BBB" in out
    assert "囁き予想の上回り +2.5" in out       # the breakdown survived the JSON
    assert ledger.verify_chain()               # invariant 2


# --- how the company's own release was cut (T-013) ---------------------------
#
# The rescue that repairs a near-miss block cuts the RELEASE's characters, so
# the excerpt is correct whether or not anything went wrong on the way. That
# is exactly why these counts have to be printed: without them a scan where
# our HTML conversion broke 13 real explanations (T-012) and a scan where
# nothing went wrong render identically.

def test_the_report_says_how_the_release_was_cut_for_each_ranked_name():
    out = render_scan_report_ja(SCAN, [make_candidate(
        "AAA", rank=1, gate_report=gate_report(),
        cause_blocks_kept=5, cause_blocks_repaired=2,
        cause_blocks_altered=1, cause_blocks_refused=3)], top_n=1)
    assert "採用 5件" in out
    assert "こちらの変換ミスを修正して採用 2件" in out
    assert "抜き出し役が語句を改変 1件" in out
    assert "原文に無く却下 3件" in out


def test_a_clean_read_still_prints_its_zeros():
    """A count that only appears when it is non-zero is a count nobody can
    trust — its absence would read as "the step is fine" and as "the step
    never ran" at the same time."""
    out = render_scan_report_ja(SCAN, [make_candidate(
        "AAA", rank=1, gate_report=gate_report(),
        cause_blocks_kept=4)], top_n=1)
    assert "採用 4件" in out
    assert "こちらの変換ミスを修正して採用 0件" in out
    assert "抜き出し役が語句を改変 0件" in out
    assert "原文に無く却下 0件" in out


def test_a_name_whose_release_was_never_read_says_so_rather_than_zero():
    """Dropped before enrichment, or no extractor key. Printing "却下0件"
    there would claim the release had been read and found clean."""
    out = render_scan_report_ja(SCAN, [make_candidate(
        "AAA", rank=1, gate_report=gate_report())], top_n=1)
    assert "この銘柄では読んでいません" in out
    assert "採用 0件" not in out


def test_an_alteration_is_called_out_not_just_counted():
    """The extractor changed a letter or a digit of the company's sentence.
    The excerpt is correct — the release's characters were used — so this
    line is the only place the user can learn it happened."""
    out = render_scan_report_ja(SCAN, [make_candidate(
        "AAA", rank=1, gate_report=gate_report(),
        cause_blocks_kept=3, cause_blocks_altered=1)], top_n=1)
    assert "改変あり" in out
    assert "hawkeye cause source AAA" in out


def test_every_row_carries_the_cut_counts_in_the_csv():
    """Not only the ranked three: which names we keep failing to read is a
    question about the whole scan."""
    text = scan_report_csv([make_candidate(
        "ZZZ", stage=ScreenedCandidateStage.GATE_REJECT,
        cause_blocks_kept=2, cause_blocks_repaired=1,
        cause_blocks_altered=0, cause_blocks_refused=4)])
    row = list(csv.DictReader(io.StringIO(text)))[0]
    assert row["発表文 採用ブロック数"] == "2"
    assert row["発表文 変換ミスを修正した数"] == "1"
    assert row["発表文 抜き出し役が改変した数"] == "0"
    assert row["発表文 却下した数"] == "4"


def test_the_scan_wide_release_tally_is_printed_even_at_zero():
    """Section ② carries this per name, but only for the tribunal names —
    and a day when nothing clears the gates is exactly a day when a defect
    on our side would run unnoticed."""
    out = render_scan_report_ja(SCAN, [make_candidate(
        "AAA", stage=ScreenedCandidateStage.GATE_REJECT,
        cause_blocks_kept=6)], top_n=3)
    assert "## ⑤ 決算発表文の読み取り" in out
    assert "そのまま採用: **6件**" in out
    assert "こちらの文章変換のミスを直して採用: **0件**" in out


def test_the_scan_wide_tally_names_the_tickers_behind_a_repair():
    out = render_scan_report_ja(SCAN, [
        make_candidate("AAA", stage=ScreenedCandidateStage.GATE_REJECT,
                       cause_blocks_kept=4, cause_blocks_repaired=2),
        make_candidate("BBB", stage=ScreenedCandidateStage.GATE_REJECT,
                       cause_blocks_kept=3, cause_blocks_altered=1),
    ], top_n=3)
    assert "こちら側の不具合の疑い**: AAA" in out
    assert "抜き出し役による改変**: BBB" in out


def test_a_scan_that_read_no_release_says_so_in_the_tally():
    out = render_scan_report_ja(SCAN, [make_candidate(
        "AAA", stage=ScreenedCandidateStage.GATE_REJECT)], top_n=3)
    assert "1銘柄も読んでいません" in out


# --- T-014: the figures behind the percentages, the outlook, what failed -----

def _table_rows(out: str) -> list[str]:
    """The data rows of §③, in the order they are printed."""
    body = out.split("## ③")[1].split("## ④")[0]
    return [line for line in body.splitlines()
            if line.startswith("|") and not line.startswith("| ---")
            and not line.startswith("| ティッカー")]


def test_the_table_shows_the_figures_each_percentage_was_computed_from():
    """A ratio alone cannot be argued with; +675% on a $0.04 consensus can."""
    out = render_scan_report_ja(SCAN, [make_candidate(
        "AAA", eps_actual=0.21, eps_estimate=0.17,
        revenue_actual=133462000.0, revenue_estimate=120880000.0)], top_n=0)
    row = _table_rows(out)[0]
    assert "0.21 / 0.17" in row
    assert "133.5M / 120.9M" in row


def test_a_revenue_in_the_billions_keeps_its_own_unit():
    out = render_scan_report_ja(SCAN, [make_candidate(
        "AAA", revenue_actual=1.504e9, revenue_estimate=1.494e9)], top_n=0)
    assert "1.50B / 1.49B" in _table_rows(out)[0]


def test_a_missing_figure_reads_as_a_dash_rather_than_a_zero():
    """Zero is a figure the company reported. A dash is us not having it."""
    out = render_scan_report_ja(SCAN, [make_candidate(
        "AAA", eps_actual=0.31, eps_estimate=None)], top_n=0)
    assert "0.31 / -" in _table_rows(out)[0]


def test_a_row_recorded_before_the_figures_were_kept_still_renders():
    out = render_scan_report_ja(SCAN, [make_candidate("AAA")], top_n=0)
    row = _table_rows(out)[0]
    assert "- / -" in row
    assert "記録なし" in row          # the guidance cell, for the same reason


@pytest.mark.parametrize("state,word", [
    (GuidanceState.DISCLOSED, "開示あり"),
    (GuidanceState.NOT_PUBLISHED, "開示なし"),
    (GuidanceState.UNREADABLE, "読めず"),
    (GuidanceState.NOT_ATTEMPTED, "未取得"),
])
def test_each_guidance_state_has_its_own_word(state, word):
    """Collapsing these to a yes/no hides our own defects inside 開示なし."""
    out = render_scan_report_ja(SCAN, [make_candidate(
        "AAA", guidance_state=state)], top_n=0)
    assert word in _table_rows(out)[0]


def test_the_vendor_is_named_rather_than_described():
    rows = [make_candidate("AAA", numbers_source="whispers"),
            make_candidate("BBB", numbers_source="calendar",
                           numbers_reason="whispers_server_error")]
    out = render_scan_report_ja(SCAN, rows, top_n=0)
    printed = _table_rows(out)
    assert "| EW |" in printed[0]
    assert "| Finnhub |" in printed[1]


def test_the_error_column_names_every_kind_that_fired():
    out = render_scan_report_ja(SCAN, [make_candidate(
        "AAA", numbers_reason="whispers_server_error",
        guidance_state=GuidanceState.UNREADABLE,
        cause_blocks_refused=2)], top_n=0)
    assert "数値・見通し・発表文" in _table_rows(out)[0]


def test_a_row_with_nothing_wrong_shows_a_dash_in_the_error_column():
    out = render_scan_report_ja(SCAN, [make_candidate(
        "AAA", guidance_state=GuidanceState.DISCLOSED)], top_n=0)
    assert "| - |" in _table_rows(out)[0]


def test_a_repaired_block_alone_is_not_called_an_error():
    """Our own conversion broke it, we repaired it, the excerpt is the
    release's own characters — §⑤ reports it; the error column does not."""
    out = render_scan_report_ja(SCAN, [make_candidate(
        "AAA", guidance_state=GuidanceState.DISCLOSED,
        cause_blocks_kept=4, cause_blocks_repaired=3)], top_n=0)
    assert "発表文" not in _table_rows(out)[0]


def test_the_table_reads_from_rank_one_down_then_by_score():
    rows = [make_candidate("LOW", score=10.0),
            make_candidate("TWO", rank=2),
            make_candidate("HIGH", score=90.0),
            make_candidate("ONE", rank=1)]
    out = render_scan_report_ja(SCAN, rows, top_n=0)
    assert [r.split("|")[1].strip() for r in _table_rows(out)] == [
        "ONE", "TWO", "HIGH", "LOW"]


def test_an_unreadable_outlook_is_explained_in_its_own_words():
    rows = [make_candidate("AAA", guidance_state=GuidanceState.UNREADABLE,
                           guidance_reason="no_summary_from_feed"),
            make_candidate("BBB", guidance_state=GuidanceState.NOT_PUBLISHED,
                           guidance_reason="no_guidance_in_source")]
    errors = render_scan_report_ja(SCAN, rows, top_n=0).split(
        "取得・読み取りに失敗したもの")[1]
    assert "AAA" in errors
    assert "no_summary_from_feed" not in errors      # translated, not raw
    assert "BBB" not in errors        # guiding nothing is not a failure


def test_a_refused_release_block_is_explained_per_ticker():
    out = render_scan_report_ja(SCAN, [make_candidate(
        "AAA", cause_blocks_kept=2, cause_blocks_refused=3,
        cause_blocks_altered=1)], top_n=0)
    errors = out.split("取得・読み取りに失敗したもの")[1]
    assert "却下 3件" in errors and "改変" in errors
    assert "hawkeye cause source AAA" in errors


def test_the_failure_section_counts_tickers_not_failures():
    """One name failing three ways is one name to look at, not three."""
    out = render_scan_report_ja(SCAN, [make_candidate(
        "AAA", numbers_reason="whispers_server_error",
        guidance_state=GuidanceState.UNREADABLE,
        guidance_reason="no_summary_from_feed",
        cause_blocks_refused=1)], top_n=0)
    assert "## ④ 取得・読み取りに失敗したもの(1銘柄)" in out


def test_the_csv_carries_the_figures_the_state_and_the_errors():
    text = scan_report_csv([make_candidate(
        "AAA", eps_actual=0.21, eps_estimate=0.17,
        revenue_actual=133462000.0, revenue_estimate=120880000.0,
        guidance_state=GuidanceState.DISCLOSED,
        numbers_reason="whispers_server_error")])
    header, row = list(csv.reader(io.StringIO(text)))[:2]
    cells = dict(zip(header, row))
    assert cells["EPS 実績/予想"] == "0.21 / 0.17"
    # Whole figures, not the screen's 133.5M: a spreadsheet has to divide them.
    assert cells["売上 実績/予想"] == "133462000 / 120880000"
    assert cells["ガイダンス"] == "開示あり"
    # A sentence, not the screen's one word: the file is read a week later
    # with no §④ beside it to explain what "数値" meant (T-018).
    assert cells["エラー"] == ("決算専門サイトからEPSまたは売上の数値が取れず、"
                              "決算カレンダーの数字で判定しました。")
    assert cells["数値の出所"] == "EW"
