"""Hawkeye CLI — the manual operating surface for the MVP.

Daily rhythm (docs/design/USER_GUIDE.ja.md):
  evaluate      run one candidate through gates -> tribunal -> risk officer
  decide        record the user's Yes/No on a proposal
  record-entry  record the fill the user executed
  check         sentinel sweep of open positions against pre-registered rules
  close         record the exit fill
  resolve-claim resolve a pre-registered claim TRUE/FALSE
  outcome       compute P&L + skill-vs-luck attribution for a closed trade
  calibration   book-level Brier / quadrant statistics
  benchmark     aggregate forward-return stats: BUY vs PASS vs gate-reject
  review-passes individual postmortem: which specific PASS/DECLINE calls
                moved a lot afterward (complements benchmark's averages)
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from datetime import date, datetime, timezone

from hawkeye.config import HawkeyeConfig
from hawkeye.paths import db_path, reports_dir
from hawkeye.envfile import load_local_env
from hawkeye.contracts.models import (
    Catalyst,
    CatalystType,
    DecisionType,
    Outcome,
    Recommendation,
    RecommendationStatus,
    ScreenedCandidateStage,
    to_jst,
    utc_date,
)
from hawkeye.ledger.scoring import (
    brier_score,
    calibration_table,
    classify_outcome,
    thesis_accuracy,
)
from hawkeye.contracts.stocks import RowStatus
from hawkeye.ledger.store import Ledger
from hawkeye.marketdata.base import CalendarUnavailable
from hawkeye.marketdata.finnhub import CompositeProvider, FinnhubProvider
from hawkeye.marketdata.snapshot import build_brief
from hawkeye.marketdata.yahoo import YahooProvider
from hawkeye.marketdata.whispers import WhispersSource
from hawkeye.reports.monitor_ja import inspection_csv
from hawkeye.reports.scan_report_ja import (
    render_scan_report_ja,
    scan_report_csv,
)
from hawkeye.reports.render_ja import (
    fmt_jst,
    render_drop_cycle_ja,
    render_drop_review_ja,
    render_recommendation_ja,
    render_backfill_ja,
    render_scout_ja,
    render_signals_ja,
)
from hawkeye.scout import cause_case, drop_case, drop_cycle, guidance_case
from hawkeye.scout.guidance_agent import (
    failure_kind as guidance_failure_kind,
    parse_reply,
)
from hawkeye.scout.cause_agent import (
    failure_kind as cause_failure_kind,
    parse_reply as parse_cause_reply,
)
from hawkeye.scout.drop_review import (
    CHECKPOINT_TRADING_DAYS,
    COHORTS,
    INVESTIGATION_COHORTS,
    attribute_by_cohort,
    attribute_by_gate,
    collect_checkpoints,
    from_recommendation,
    from_screened,
    is_reviewable,
    outliers,
    to_drop_review,
    with_peer_baseline,
)
from hawkeye.scout.benchmark import (
    cohort_stats,
    collect_samples,
    forward_return,
    min_calendar_days_for_trading_days,
    reason_snippet,
)
from hawkeye.ledger.stocks import StockStore
from hawkeye.marketdata.edgar import EdgarDirectory
from hawkeye.reports.quality_ja import (
    render_quality_ja,
    render_stock_history_ja,
)
from hawkeye.scout.single import StoredPrintMismatch, judge_ticker
from hawkeye.scout.drift import (
    DriftStatus,
    measure_consensus_drift,
)
from hawkeye.scout.backfill import backfill_history
from hawkeye.scout.drift import report_line as report_drift_line
from hawkeye.scout.prereg import (
    capture_consensus,
    capture_window,
    report_line,
    upcoming_prints,
    warn_if_nothing_captured,
)
from hawkeye.scout.scout import (
    build_screened_candidates,
    rerank_after_guidance,
    run_scout,
    scan_window,
)
from hawkeye.scout import scan_store
from hawkeye.scout.quality import assess_earnings
from hawkeye.scout.triage import rebuild_triage
from hawkeye.sentinel.monitor import check_position
from hawkeye.tribunal import casefile
from hawkeye.tribunal.pipeline import (
    gate_only_recommendation,
    run_tribunal,
)
from hawkeye.gates.entry_gates import run_entry_gates


def _provider() -> CompositeProvider:
    return CompositeProvider(YahooProvider(), FinnhubProvider())


def _ledger() -> Ledger:
    return Ledger(db_path())


def _stock_store() -> StockStore:
    """The stock master lives in the same database as the ledger; its tables
    are additive and outside the hash chain (§6.1)."""
    return StockStore(db_path())


def cmd_evaluate(args: argparse.Namespace) -> int:
    config = HawkeyeConfig.from_env()
    catalyst = Catalyst(
        type=CatalystType(args.catalyst),
        description=args.description,
        event_date=date.fromisoformat(args.event_date),
        source=args.source or "manual",
    )
    overrides = {
        "price": args.price,
        "market_cap": args.market_cap,
        "avg_dollar_volume_20d": args.adv,
        "atr_pct_14d": args.atr_pct,
        "gap_on_event_pct": args.gap_pct,
        "days_since_event": args.days_since_event,
        "eps_surprise_pct": args.eps_surprise_pct,
        "revenue_surprise_pct": args.revenue_surprise_pct,
    }
    brief = build_brief(args.ticker.upper(), catalyst, _provider(),
                        notes=args.notes or "", overrides=overrides,
                        config=config)

    from hawkeye.tribunal.llm import AnthropicLLM
    llm = AnthropicLLM(model=config.model)

    ledger = _ledger()
    open_count = len(ledger.open_positions())
    rec = run_tribunal(brief, llm, config, nav=args.nav,
                       open_position_count=open_count)
    status = (RecommendationStatus.PROPOSED
              if rec.verdict.decision == DecisionType.BUY
              else RecommendationStatus.SYSTEM_PASS)
    ledger.record_recommendation(rec, status)
    print(render_recommendation_ja(rec))
    report_path = _write_tribunal_report(rec)
    print(f"\n(記録済み: {rec.id} / status={status.value} / DB={db_path()})")
    print(f"(レポート保存先: {report_path})")
    return 0


def _print_step(case: "casefile.Case") -> None:
    if case.recommendation_id is not None:
        print(f"case: {case.id}  status: complete  "
              f"recommendation: {case.recommendation_id}")
        print(f"view with: hawkeye show {case.recommendation_id}")
        return
    package = casefile.write_package(case)
    print(f"case: {case.id}  ticker: {case.brief.ticker}")
    print(f"next_role: {package['role']}")
    print(f"system: {package['system']}")
    print(f"input: {package['input']}")
    print(f"schema: {package['schema']}")
    print(f"write_reply_to: {package['output']}")
    print(f"submit_with: hawkeye case submit {case.id} --file {package['output']}")


def _rounded(value):
    return round(value, 1) if value is not None else None


def _cause_source(feed, model: str = ""):
    """The release reader, wired from the environment (T-008).

    Built here rather than inline so the scan and the inspection command
    below cannot drift into reading different text — the whole value of the
    command is that it shows what the scan would stage.

    `model` overrides the extractor for one invocation and is never set by
    the scan: the scan must read what the doctrine says it reads. It exists
    so a person can put two models on the same release and compare, which is
    how the 2.5-to-3.5 switch was decided (T-011).
    """
    import os

    from hawkeye.marketdata.gemini import GeminiExtractor
    from hawkeye.scout.cause_source import ReleaseCauseSource

    key = os.environ.get("GEMINI_API_KEY", "")
    extractor = (GeminiExtractor(api_key=key, model=model) if model
                 else GeminiExtractor(api_key=key))
    return ReleaseCauseSource(feed, extractor)


def cmd_cause_source(args: argparse.Namespace) -> int:
    """Show what the cause reader would be given for one named stock.

    The reason a quarter came out where it did is not in the vendor's summary
    — 0 of 30 prints yielded one (measured 2026-08-17) — so it now comes from
    the company's own earnings release. This command is how that is checked
    without paying for a scan: it prints the release's size, the excerpt cut
    from it, and every block the extractor returned that the release does NOT
    contain.

    That last list is the point. On 2026-08-17 the extractor composed a
    fluent sentence for AII, under explicit instruction to copy character for
    character. Blocks that fail the check are shown rather than counted, so a
    reader inventing text is visible to a person and not just to a metric.
    """
    ticker = args.ticker.strip().upper()
    feed = WhispersSource()
    source = _cause_source(feed, getattr(args, "model", "") or "")
    if not source.available:
        print("GEMINI_API_KEY が設定されていません（.env.local）。決算理由の"
              "抜き出しには必要です。", file=sys.stderr)
        return 1

    record = feed.details(ticker)
    if record is None:
        print(f"{ticker}: 決算フィードにこの銘柄の記録がありません。",
              file=sys.stderr)
        return 1

    quarter = record.fiscal_quarter or ""
    built = source.text_for(ticker, record.file_name, quarter)
    print(f"{ticker}  {quarter}  発表 "
          f"{record.announced_at.date() if record.announced_at else '不明'}")
    print(f"  ベンダー要約: {len(record.summary)}文字")
    print(f"  決算発表文  : {len(built.source_text)}文字 "
          f"(記事ID {record.file_name or 'なし'})")
    if built.repaired:
        # Our own text conversion, not the extractor's doing. Shown because
        # this is the only place it surfaces: the excerpt is correct, so a
        # conversion defect would otherwise be invisible until someone
        # measured the refusal rate by hand (which is how T-012 was found).
        print(f"\nこちらの変換ミスを直して採用したブロック "
              f"{len(built.repaired)}件（原文とは空白・記号だけの差）")
    if built.altered:
        print(f"\n抜き出し役が会社の語句を書き換えたブロック "
              f"{len(built.altered)}件（審理に渡すのは原文の文字の方です）:")
        for sent, actual in built.altered:
            print(f"  抜き出し役: {sent}")
            print(f"  発表文    : {actual}")
    if built.rejected:
        print(f"\n原文のどこにも近い箇所が無く却下したブロック "
              f"{len(built.rejected)}件:")
        for block in built.rejected:
            print(f"  ✗ {block}")
    if not built.excerpt:
        print(f"\n読み手に渡せる本文はありません: {built.reason}")
        if built.detail:
            # What the failing step said. Without it the line above is the
            # whole record, and "extractor_call_failed" has been the entire
            # evidence behind two wrong conclusions about a rate limit whose
            # answer was in the discarded reply (T-011).
            print(f"  失敗の内容: {built.detail}")
        return 0
    print(f"\n読み手に渡す抜粋 {len(built.excerpt)}文字 "
          f"(発表文の{len(built.excerpt) * 100 // max(len(built.source_text), 1)}%):")
    print(built.excerpt)
    return 0


def _judged_earnings(args: argparse.Namespace):
    """The three-leg reading of a named stock's latest quarter, or None.

    Same path the funnel uses — same earnings feed, same one-vendor-per-print
    rule, same pinned consensus — so a stock a person named arrives at the
    tribunal on exactly the evidence a discovered one would.
    """
    finnhub = FinnhubProvider()
    if not finnhub.available:
        print("--from-earnings には FINNHUB_API_KEY が必要です", file=sys.stderr)
        return None
    numbers = WhispersSource()
    return judge_ticker(
        args.ticker, finnhub, HawkeyeConfig.from_env(),
        report_date=(date.fromisoformat(args.event_date)
                     if args.event_date else None),
        numbers_source=numbers,
        stock_store=_stock_store(), directory=EdgarDirectory())


# What a ticker may contribute to a filename. Tickers reach us as whatever
# the earnings calendar and the earnings feed wrote, uppercased and trimmed
# and checked no further, and a class share is written `BRK.A` by one vendor
# and `BF/B` by another. Windows refuses \ / : * ? " < > | in a name outright,
# so an unfiltered ticker can make the save raise and cost the round the only
# document it leaves behind. Substituting keeps the document (User decision
# 2026-08-20); the dot is kept because it is legal and it is part of the name.
_UNSAFE_IN_FILENAME = re.compile(r"[^A-Z0-9._]", re.IGNORECASE)


def _ticker_slug(ticker: str) -> str:
    """The ticker in a form a filesystem will accept, unchanged where it can."""
    return _UNSAFE_IN_FILENAME.sub("-", ticker.strip().upper())


def _write_tribunal_report(rec: Recommendation) -> pathlib.Path:
    """Save the rendered report to disk so a completed round leaves a
    document behind, not just terminal output that scrolls away.

    The name carries the ticker beside the second, and an existing file is
    never opened for writing. Both halves answer one defect: two rounds
    finishing inside the same second resolved to one path, and the later save
    erased the earlier round's only document in silence — RNW's report was
    lost to PONY's on 2026-08-19, and nothing on screen said so (T-017).
    """
    out_dir = reports_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = to_jst(datetime.now(timezone.utc)).strftime("%y%m%d-%H%M%S")
    base = f"{stamp}-{_ticker_slug(rec.ticker)}-tribunal-report"
    text = render_recommendation_ja(rec)
    attempt = 1
    while True:
        suffix = "" if attempt == 1 else f"-{attempt}"
        path = out_dir / f"{base}{suffix}.md"
        try:
            # "x" refuses a name already taken instead of truncating it, and
            # it decides that in the same step as the write — a check made
            # first would leave a gap for the other round to write into.
            with path.open("x", encoding="utf-8") as handle:
                handle.write(text)
            return path
        except FileExistsError:
            attempt += 1


def _print_stored_print_mismatch(mismatch: StoredPrintMismatch) -> None:
    """Both readings of the figure, side by side, and no case.

    Either could be the corrected one — a vendor restatement or a feed
    glitch — and picking a side here would decide the tribunal's evidence
    silently. The human chooses (User decision, 2026-08-17, T-006).
    """
    print(f"{mismatch.ticker} {mismatch.fiscal_quarter}: 銘柄マスタに保存済みの"
          f"決算行と、決算カレンダー/フィードから今回取り直した数値が"
          f"食い違っています。", file=sys.stderr)
    for d in mismatch.differences:
        print(f"  {d.field}: 保存済み {d.stored!r} / 今回取得 {d.fetched!r}",
              file=sys.stderr)
    print("案件は開きません。どちらが正しいかは自動で選ばず、人間の確認に"
          "委ねます。ベンダーの実績値訂正であれば、発表からの監視期間内に"
          "`hawkeye scout` を再実行すると訂正として取り込まれます(task 8.5)。",
          file=sys.stderr)


def cmd_case_open(args: argparse.Namespace) -> int:
    config = HawkeyeConfig.from_env()
    try:
        judged = _judged_earnings(args) if args.from_earnings else None
    except StoredPrintMismatch as mismatch:
        _print_stored_print_mismatch(mismatch)
        return 1
    if args.from_earnings:
        if judged is None:
            print(f"{args.ticker}: 決算カレンダーに実績のある行が見つかりません"
                  f"(--event-date で発表日を指定してください)", file=sys.stderr)
            return 1
        print(render_quality_ja(judged.quality))
        args.description = judged.catalyst_description
        args.event_date = judged.event.day.isoformat()
        args.source = args.source or "scout/finnhub-earnings-calendar"
        # Only a confirmed beat becomes a structured fact. The prompts tell
        # both roles to prefer these over prose, so an unverified leg placed
        # here would be laundered into a fact.
        args.eps_surprise_pct = _rounded(judged.quality.eps.scored_pct)
        args.revenue_surprise_pct = _rounded(judged.quality.revenue.scored_pct)
    elif not (args.description and args.event_date):
        print("--description と --event-date が必要です"
              "(または --from-earnings で決算から生成してください)",
              file=sys.stderr)
        return 1
    catalyst = Catalyst(
        type=CatalystType(args.catalyst),
        description=args.description,
        event_date=date.fromisoformat(args.event_date),
        source=args.source or "manual",
    )
    overrides = {
        "price": args.price,
        "market_cap": args.market_cap,
        "avg_dollar_volume_20d": args.adv,
        "atr_pct_14d": args.atr_pct,
        "gap_on_event_pct": args.gap_pct,
        "days_since_event": args.days_since_event,
        "eps_surprise_pct": args.eps_surprise_pct,
        "revenue_surprise_pct": args.revenue_surprise_pct,
    }
    brief = build_brief(args.ticker.upper(), catalyst, _provider(),
                        notes=args.notes or "", overrides=overrides,
                        config=config)
    gates = run_entry_gates(brief.snapshot, catalyst, config)
    ledger = _ledger()
    if not gates.ok:
        rec = gate_only_recommendation(brief, gates)
        ledger.record_recommendation(rec, RecommendationStatus.SYSTEM_PASS)
        print(render_recommendation_ja(rec))
        report_path = _write_tribunal_report(rec)
        print(f"\n(ゲートで却下 — LLM不要。記録済み: {rec.id})")
        print(f"(レポート保存先: {report_path})")
        return 0
    case = casefile.open_case(brief, gates, nav=args.nav,
                              open_position_count=len(ledger.open_positions()))
    _print_step(case)
    return 0


def cmd_case_step(args: argparse.Namespace) -> int:
    try:
        case = casefile.load_case(args.case_id)
    except FileNotFoundError:
        print(f"case not found: {args.case_id}", file=sys.stderr)
        return 1
    _print_step(case)
    return 0


def _case_finalize_and_record(case: "casefile.Case", config: HawkeyeConfig) -> int:
    rec = casefile.finalize(case, config)
    ledger = _ledger()
    status = (RecommendationStatus.PROPOSED
              if rec.verdict.decision == DecisionType.BUY
              else RecommendationStatus.SYSTEM_PASS)
    ledger.record_recommendation(rec, status)
    casefile.mark_complete(case, rec.id)
    print()
    print(render_recommendation_ja(rec))
    report_path = _write_tribunal_report(rec)
    print(f"\n(記録済み: {rec.id} / status={status.value})")
    print(f"(レポート保存先: {report_path})")
    return 0


def cmd_case_submit(args: argparse.Namespace) -> int:
    import json as _json
    try:
        case = casefile.load_case(args.case_id)
    except FileNotFoundError:
        print(f"case not found: {args.case_id}", file=sys.stderr)
        return 1
    config = HawkeyeConfig.from_env()

    if casefile.next_role(case) is None and case.recommendation_id is None:
        # All three roles were submitted in a previous run, but recording
        # into the ledger never got confirmed (e.g. it crashed or the DB
        # was locked) — finish that instead of erroring "case already
        # complete" on a case that never actually finished.
        print("all roles already submitted; retrying ledger recording")
        return _case_finalize_and_record(case, config)

    try:
        payload = _json.loads(open(args.file, encoding="utf-8").read())
    except (OSError, _json.JSONDecodeError) as exc:
        print(f"cannot read JSON from {args.file}: {exc}", file=sys.stderr)
        return 1
    try:
        role = casefile.submit(case, payload)
    except (ValueError, KeyError, TypeError) as exc:
        print(f"submission rejected ({exc}) — fix the JSON and resubmit",
              file=sys.stderr)
        return 1
    print(f"accepted: {role}")
    if casefile.next_role(case) is None:
        return _case_finalize_and_record(case, config)
    _print_step(case)
    return 0


def cmd_case_list(args: argparse.Namespace) -> int:
    # Housekeeping runs here rather than as a command to remember: this is
    # the one command /hawkeye-run always calls first (§5.2(7)/(8)). Reported
    # rather than silent — a cleanup nobody sees is one nobody can question.
    swept = casefile.sweep_role_workspaces()
    if swept:
        print(f"(完了済みケースの作業ファイルを削除: {len(swept)}件)")
    cases = casefile.list_cases()
    if not cases:
        print("(ケースなし)")
        return 0
    for c in cases:
        state = (f"complete -> {c.recommendation_id}"
                 if c.recommendation_id else
                 f"awaiting {casefile.next_role(c)}")
        print(f"{c.id}  {c.brief.ticker:<6}  {state}")
    return 0


def _backfill_targets(store, passed: list, top_n: int) -> list[tuple[str, str]]:
    """(ticker, stock_id) for the names whose history is worth filling in.

    A candidate carries no stock id — the master is keyed by CIK, and the
    lookup is by ticker. A name with no master row is skipped rather than
    created here: the master is written by the scan itself, and inventing a
    row from a backfill would put a company on record that nothing has
    verified is the company we think it is.
    """
    targets = []
    for candidate in passed[:top_n]:
        stock = store.stock_by_ticker(candidate.ticker)
        if stock is not None:
            targets.append((candidate.ticker, stock.id))
    return targets


def cmd_scout(args: argparse.Namespace) -> int:
    """Scan only — gates, but no score, no sort, no ledger write.

    A quarter's guidance leg (docs/design/RANK_AFTER_GUIDANCE.ja.md) cannot be
    known at scan time: nothing inside this process can call the agent that
    reads it, so `run_scout` stages the sentence to `var/guidance/` and
    scores every judged candidate as if the company had guided nothing. That
    score is provisional. Committing a shortlist decided on it would be
    ranking on incomplete data — so this command writes the whole result to
    `var/scan/` instead of the ledger, and `hawkeye rank` is what scores it
    for real, once the guidance queue is empty.
    """
    if scan_store.has_pending_scan():
        print("前回の走査がまだ順位付けされていません。順位付け(hawkeye rank)"
              "を先に済ませるか、見通しの読み取り待ちがあれば "
              "hawkeye guidance queue で埋めてください。", file=sys.stderr)
        return 1
    config = HawkeyeConfig.from_env()
    finnhub = FinnhubProvider()
    if not finnhub.available:
        print("scout には FINNHUB_API_KEY が必要です(無料キー: finnhub.io)",
              file=sys.stderr)
        return 1
    provider = CompositeProvider(YahooProvider(), finnhub)
    ledger = _ledger()
    today = date.today()
    # The window is derived from the previous run, so an irregular manual
    # cadence doesn't leave unscanned days (§5.2(1)). --days forces a fixed
    # lookback instead, for backfills and one-off exploration.
    window = (None if args.days
              else scan_window(today, ledger.last_scan_at(), config))
    # Discovery stays with the calendar; every number the ranking rests on is
    # read from the earnings feed before the shortlist is decided
    # (hawkeye/scout/numbers.py). A name the feed cannot answer for keeps the
    # calendar's own figures, for BOTH legs, and says so.
    numbers = WhispersSource()
    # Why the quarter came out where it did is NOT in the feed's summary — 0
    # of 30 prints yielded a reason from it (measured 2026-08-17) — so the
    # cause queue is fed from the company's own earnings release, cut to the
    # blocks that explain it and then checked back against the release
    # (T-008, hawkeye/scout/cause_source.py). With no GEMINI_API_KEY the
    # source reports itself unavailable and the scan falls back to the
    # summary exactly as before: a scan must not stop because an optional
    # reader has no key, but it must say which text it ended up reading.
    cause_reader = _cause_source(numbers)
    if not cause_reader.available:
        print("注意: GEMINI_API_KEY が無いため、決算理由は従来どおりベンダー"
              "要約から読み取ります（実測 30件中0件）。", file=sys.stderr)
    # The stock master turns each print into a stored quarter judged against
    # the consensus that was in force before it. Without a pre-registered row
    # the funnel reconstructs one and says so — it never silently treats an
    # after-the-fact estimate as what was knowable in advance (§6.1(D)).
    # A calendar that cannot answer ends the run here, before record_scan()
    # in cmd_rank. The next window starts from the last recorded scan, so
    # writing one for a window nobody managed to read would put those days
    # out of reach permanently — and the funnel would have printed them as a
    # quiet market (found live 2026-08-03).
    store = _stock_store()
    try:
        result = run_scout(finnhub, provider, config, days_back=args.days,
                           window=window, already_seen=ledger.seen_events(),
                           today=today,
                           numbers_source=numbers,
                           stock_store=store,
                           directory=EdgarDirectory(),
                           cause_source=cause_reader)
    except CalendarUnavailable as exc:
        print(f"決算カレンダーを読めなかったため、走査を中止しました: {exc}",
              file=sys.stderr)
        print("走査は記録していないため、復旧後に再実行すれば同じ期間を"
              "読み直します。", file=sys.stderr)
        return 1

    scan_store.save_scan_result(result)
    print(render_scout_ja(result))
    if args.monitor_csv:
        # Written with a BOM: Excel on Japanese Windows reads a plain UTF-8 CSV
        # as cp932 and turns every header into mojibake, which makes the check
        # sheet unusable for the one person it is written for.
        path = pathlib.Path(args.monitor_csv)
        path.write_text(inspection_csv(result.inspection), encoding="utf-8-sig")
        print(f"\n点検表をCSVに保存しました: {path} "
              f"({len(result.inspection.rows)}行)")
    print(f"\n(この走査はまだ台帳に記録していません。会社の見通しの読み取り待ちが"
          f"{result.guidance.staged}件あります。"
          f"hawkeye guidance queue で読み切ってから hawkeye rank を"
          f"実行してください。)")
    return 0


def _unread_guidance_ja(
        unread: list[guidance_case.GuidanceCase]) -> str:
    """Why the ranking stopped, and the two ways forward.

    Names every ticker rather than a bare count: the operator's next move is
    `hawkeye guidance queue`, and knowing WHICH company is waiting is what
    tells them whether the reading is one they already tried and failed.
    """
    names = "、".join(c.ticker for c in unread)
    return "\n".join([
        f"会社の見通し(ガイダンス)の読み取り待ちが {len(unread)}件 "
        f"あります({names})。",
        "このまま順位付けすると、その銘柄の見通しの点数がゼロのまま順位が"
        "決まり、審理に送る銘柄の顔ぶれが変わります。",
        "  hawkeye guidance queue   で読み切ってから hawkeye rank を"
        "実行してください。",
        "  読み取りがどうしても通らない場合のみ:",
        "  hawkeye rank --allow-unread-guidance"
        "  (見通しを読まずに順位を決めたことが台帳に残ります)",
    ])


def _ranked_unread_ja(
        unread: list[guidance_case.GuidanceCase]) -> str:
    """The escape hatch, said out loud. Printed whenever it is taken."""
    names = "、".join(c.ticker for c in unread)
    return "\n".join([
        f"⚠️ 見通しを読まずに順位を決めました: 読み取り待ち {len(unread)}件"
        f"({names})を残したまま --allow-unread-guidance で実行しています。",
        "   この銘柄は見通しの点数がゼロのまま順位付けされています。"
        "台帳の走査記録にも同じことを書きました。",
    ])


def cmd_rank(args: argparse.Namespace) -> int:
    """Re-score the pending scan now that guidance can actually be known, sort
    it, and only THEN commit it to the ledger (docs/design/RANK_AFTER_GUIDANCE.ja.md).

    This is the one code path both engines share for deciding the shortlist:
    the score `hawkeye scout` computed was provisional (guidance unread), so
    recomputing it here — after `hawkeye guidance queue` / `submit` has run —
    is what keeps API mode and session mode selecting the same names off the
    same numbers, rather than session mode ranking on a guidance leg that
    reads as "the company said nothing" every time.
    """
    if not scan_store.has_pending_scan():
        print("順位付け待ちの走査がありません。先に hawkeye scout を"
              "実行してください。", file=sys.stderr)
        return 1
    # The one leg that can still move between `scout` and here (T-016). Both
    # `scout`'s closing line and `hawkeye guidance queue` already SAY the
    # readings are outstanding, but saying it is advice, and CLAUDE.md
    # invariant 3 asks code to enforce what the prompt requests: until this
    # guard existed the whole shortlist could be scored with that leg at zero
    # for every name, sorted on it, and committed — silently. Checked before
    # anything is loaded or written, so a refusal costs nothing and leaves the
    # pending scan exactly where it was.
    unread = guidance_case.list_cases()
    if unread and not args.allow_unread_guidance:
        print(_unread_guidance_ja(unread), file=sys.stderr)
        return 1
    config = HawkeyeConfig.from_env()
    finnhub = FinnhubProvider()
    store = _stock_store()
    ledger = _ledger()
    result = scan_store.load_scan_result()
    rerank_after_guidance(store, result, config)
    if unread:
        # Twice, on purpose, and for two different readers: stderr now, before
        # the several hundred lines of report that would bury it, and stdout
        # at the very end (below), which is what gets scrolled back to and
        # what a redirected run keeps on disk.
        print(_ranked_unread_ja(unread), file=sys.stderr)

    # Whatever isn't sent to the tribunal THIS run — from result.passed's
    # tail onward — is the ranking-cutoff tier (docs/design/MASTER_OVERVIEW.ja.md
    # §5.1, #4). Computed before record_scan() so it can be persisted in the
    # same breath as the scan itself, immediately below.
    sent_to_tribunal_n = max(args.evaluate or 0, args.open_cases or 0)

    scan_id = ledger.record_scan(
        params={"window_start": result.scan_start.isoformat(),
                "window_end": result.scan_end.isoformat(),
                "window_truncated": result.window_truncated,
                # Not preserved across the scan/rank split — see
                # hawkeye/scout/scan_store.py. Purely descriptive; nothing
                # downstream reads this key.
                "days_back_override": None,
                "duplicates_skipped": result.duplicates,
                "min_eps_surprise": config.scout_min_eps_surprise_pct,
                # Recorded on every scan, zero included: a key that only
                # appears when something went wrong cannot be told apart from
                # a row written before the key existed (T-016).
                "ranked_with_unread_guidance": bool(unread),
                "guidance_unread_at_rank": len(unread),
                "guidance_unread_tickers": [c.ticker for c in unread],
                **result.numbers.as_dict()},
        scanned=result.scanned, screened=result.screened,
        enriched=result.enriched, gate_passed=len(result.passed),
        tickers=[c.ticker for c in result.passed])
    ledger.record_screened_candidates(
        scan_id, build_screened_candidates(result, scan_id, sent_to_tribunal_n))

    # The quarters before this one, for the shortlist only (task 10). One
    # request per name, so this runs AFTER the ranking rather than over every
    # screened name: filling in history for companies no argument will be made
    # about would cost hundreds of requests for nothing.
    backfill = backfill_history(
        store, finnhub,
        _backfill_targets(store, result.passed,
                          max(sent_to_tribunal_n, config.scout_backfill_top_n)),
        quarters=config.scout_backfill_quarters)

    print(render_scout_ja(result))
    summary = render_backfill_ja(backfill)
    if summary:
        print()
        print(summary)

    judged = [c for c in result.passed if c.quality is not None]
    if judged:
        print("\n## 決算の中身(EPS・売上・ガイダンスの3本柱)")
        for candidate in judged[:max(sent_to_tribunal_n, 5)]:
            print(render_quality_ja(candidate.quality))
            print()

    if args.open_cases and result.passed:
        print("\n## セッションモード用ケース(/hawkeye-run が処理します)")
        open_count = len(ledger.open_positions())
        for candidate in result.passed[:args.open_cases]:
            case = casefile.open_case(candidate.brief, candidate.gate_report,
                                      nav=args.nav,
                                      open_position_count=open_count)
            print(f"- {case.id}  {candidate.ticker}  (score {candidate.score})")
        print("次: hawkeye case step <case_id>")

    if args.evaluate and result.passed:
        from hawkeye.tribunal.llm import AnthropicLLM
        llm = AnthropicLLM(model=config.model)
        open_count = len(ledger.open_positions())
        for candidate in result.passed[:args.evaluate]:
            print(f"\n{'=' * 70}\n審理中: {candidate.ticker} ...\n")
            rec = run_tribunal(candidate.brief, llm, config, nav=args.nav,
                               open_position_count=open_count)
            status = (RecommendationStatus.PROPOSED
                      if rec.verdict.decision == DecisionType.BUY
                      else RecommendationStatus.SYSTEM_PASS)
            ledger.record_recommendation(rec, status)
            print(render_recommendation_ja(rec))
            report_path = _write_tribunal_report(rec)
            print(f"\n(記録済み: {rec.id} / status={status.value})")
            print(f"(レポート保存先: {report_path})")

    if unread:
        print()
        print(_ranked_unread_ja(unread))

    scan_store.discard_scan_result()
    return 0


def cmd_consensus_capture(args: argparse.Namespace) -> int:
    """Pre-register the consensus for prints due in the next few days.

    Run this BEFORE the releases. Afterwards no second source for consensus
    exists anywhere, so a day skipped here is a snapshot that can never be
    taken — which is also why the window is two business days rather than
    "tomorrow" (§6.1(D)).
    """
    config = HawkeyeConfig.from_env()
    finnhub = FinnhubProvider()
    if not finnhub.available:
        print("コンセンサスの事前登録には FINNHUB_API_KEY が必要です",
              file=sys.stderr)
        return 1
    days = args.days or config.consensus_capture_business_days
    today = date.today()
    store = _stock_store()
    # Normally tomorrow onward. After a gap the window also covers today,
    # whose US prints land this evening JST and would otherwise fall between
    # two runs permanently (§6.1(D)).
    window = capture_window(
        today, store.last_pre_registration_at(), business_days=days,
        gap_days=config.consensus_capture_include_today_after_days)
    include_today = window[0] == today
    raw = finnhub.earnings_calendar(window[0], window[-1])
    targets = upcoming_prints(raw, today=today, business_days=days,
                              include_today=include_today)
    if args.limit:
        targets = targets[:args.limit]
    print(f"対象期間: {window[0]} 〜 {window[-1]}(営業日{days}日)"
          + ("(前回の事前登録から日が空いているため、本日発表分も対象に"
             "含めています)" if include_today else ""))
    print(f"決算予定で実績がまだ出ていない銘柄: {len(targets)} 件")
    if args.dry_run:
        for item in targets:
            # An empty label means no source stated the quarter, and
            # `capture_consensus` will refuse the row — say so here rather
            # than print a blank column that reads as a formatting glitch.
            print(f"- {item.report_date} {item.ticker} "
                  f"{item.fiscal_quarter or '四半期不明のため記録しません'} "
                  f"(EPS予想 {item.eps_estimate})")
        print("(--dry-run のため記録していません)")
        return 0

    # The same vendor that will later supply the actual. That is the point of
    # it: a surprise ratio whose consensus and actual come from different
    # vendors is arithmetic without a referent.
    report = capture_consensus(store, targets, WhispersSource(),
                               directory=EdgarDirectory(),
                               today=today, config=config)
    print(report_line(report))
    warn_if_nothing_captured(report)
    return 0


def cmd_consensus_drift(args: argparse.Namespace) -> int:
    """Compare each pre-registered consensus against what the feed says now.

    A measurement, not a capture: nothing is written. It answers one question
    — whether pre-registration still buys anything now that the feed states
    the reported quarter's own consensus after the print — and the answer
    decides whether ~600 requests a run keep being spent on it.

    Run it AFTER the prints it covers have landed. Run it soon, too: the feed
    keeps only a company's latest print, so a row becomes unmeasurable the
    moment that company reports again.
    """
    store = _stock_store()
    report = measure_consensus_drift(store, WhispersSource(),
                                     today=date.today(), limit=args.limit)
    for reading in report.readings:
        if args.only_moved and reading.status is not DriftStatus.MOVED:
            continue
        gap = (f" ({reading.days_apart}日前に記録)"
               if reading.days_apart is not None else "")
        print(f"- {reading.ticker} {reading.fiscal_quarter} "
              f"{reading.status.value}{gap}")
        if reading.status in (DriftStatus.MOVED, DriftStatus.UNCHANGED):
            print(f"    EPS 決算前 {reading.eps_before} → 決算後 "
                  f"{reading.eps_after} "
                  f"({_rounded(reading.eps_drift_pct)}%)")
            print(f"    売上 決算前 {reading.revenue_before} → 決算後 "
                  f"{reading.revenue_after} "
                  f"({_rounded(reading.revenue_drift_pct)}%)")
    print(report_drift_line(report))
    if report.compared == 0:
        print("(事前登録を一度も走らせていないか、対象の決算がまだ出ていません。"
              "hawkeye consensus capture を決算の前日に走らせ、決算が出た"
              "翌日にこのコマンドを実行してください)", file=sys.stderr)
    return 0


def cmd_stocks_show(args: argparse.Namespace) -> int:
    store = _stock_store()
    stock = store.stock_by_ticker(args.ticker)
    if stock is None:
        print(f"{args.ticker.upper()}: 銘柄マスタに記録がありません "
              f"(まだ一度も探索に上がっていない銘柄です)")
        return 1
    history = store.history(stock.id)
    print(render_stock_history_ja(history))
    return 0


def cmd_stocks_list(args: argparse.Namespace) -> int:
    rows = _stock_store().stocks()
    if not rows:
        print("(銘柄マスタは空です。hawkeye scout か hawkeye consensus capture "
              "を実行すると作られます)")
        return 0
    for stock in rows:
        reviewed = (f"{stock.last_reviewed_fiscal_quarter}"
                    f"({stock.last_stage_reached.value})"
                    if stock.last_stage_reached else "未審査")
        print(f"{stock.ticker:<8} {stock.id:<18} {reviewed:<24} {stock.name}")
    print(f"\n計 {len(rows)} 銘柄")
    return 0


def cmd_stocks_rebuild(args: argparse.Namespace) -> int:
    """Rebuild the master's review projection from the ledger.

    The projection is a cache. If it ever disagrees with the ledger the
    ledger is right, and this is how that is restored.
    """
    store = _stock_store()
    rebuilt = store.rebuild_projection(_ledger())
    print(f"台帳から {rebuilt} 銘柄の審査状況を作り直しました")
    # The same rule applies to the "worth following at all" verdict: it is
    # read off the entry-gate reports already frozen into the drop records,
    # so it must be rebuildable from them rather than only accumulating as
    # the funnel happens to run.
    triaged = rebuild_triage(store)
    print(f"入口ゲートの記録から {triaged} 銘柄の調査対象判定を作り直しました")
    return 0


def cmd_stocks_prune_revisions(args: argparse.Namespace) -> int:
    """Physically remove retired print rows (task 8.5, step 5).

    Retiring is what a revision does on its own; this is the separate,
    explicit step the design asks for, because the retired row is the only
    record of the figure a past ranking was actually made on. Previews unless
    `--apply` is given.
    """
    store = _stock_store()
    retired = [(s, p) for s in store.stocks()
               for p in store.prints(s.id)
               if p.status is RowStatus.SUPERSEDED
               and (args.ticker is None or s.ticker == args.ticker)]
    if not retired:
        print("訂正で古くなった決算行はありません。")
        return 0
    print(f"# 訂正で古くなった決算行 ({len(retired)}件)\n")
    print("| 銘柄 | 四半期 | 決算日 | EPS実績 | 売上実績 |")
    print("|---|---|---|---|---|")
    for stock, row in retired:
        eps = row.eps_actual if row.eps_actual is not None else \
            row.eps_actual_rows_usable
        print(f"| {stock.ticker} | {row.fiscal_quarter} | {row.report_date} "
              f"| {'-' if eps is None else f'{eps:g}'} "
              f"| {'-' if row.revenue_actual is None else f'{row.revenue_actual:,.0f}'} |")
    print()
    if not args.apply:
        print("※ これは下見です。実際には削除していません。"
              "これらを消すと「その時どの数値で順位を付けたか」が"
              "たどれなくなります。削除するには --apply を付けてください。")
        return 0
    removed = sum(store.delete_superseded_prints(s.id)
                  for s in {stock.id: stock for stock, _ in retired}.values())
    print(f"{removed}件を削除しました。現行の行には触れていません。")
    return 0


# The two refusals worth re-reading, and the reason the distinction is not a
# new list: `reader_failed` means the reply broke a MECHANICAL check of ours
# (the quote is not in the release, the unit is not one we accept, the period
# is unreadable) and `call_failed` means the call never completed. Both are
# ours to fix and both are retryable. Everything else — chiefly the reader
# reporting that the release states no reason — is a final answer, and keeping
# it staged would invite a reworded retry, which is exactly what the run skill
# forbids. The mapping itself stays in each gate's own `_FAILURE_KIND`.
_RETRYABLE_REFUSALS = ("reader_failed", "call_failed")


def _keep_staged(reason: str, classify) -> bool:
    """Whether a refusal leaves the material where the reader can try again."""
    return bool(reason) and classify(reason) in _RETRYABLE_REFUSALS


def _report_kept_package(ticker: str, reason: str, queue_command: str,
                         case_id: str) -> None:
    """Say plainly that nothing was lost, and what to do next.

    Before T-015 this path deleted the staged summary on its way out, so a
    reply that merely used the wrong word for a unit ended the reading for
    that whole scan — AMBQ, 2026-08-18.
    """
    print(f"{ticker}: 読み取り結果は形式検査を通りませんでした"
          f"(理由: {reason})。材料は残してあるので、指示文を読み直して"
          "同じ case-id で再提出してください:", file=sys.stderr)
    print(f"  hawkeye {queue_command} queue --case-id {case_id}",
          file=sys.stderr)


def _print_reader_package(package: dict, case, submit_command: str) -> None:
    """Name the four files a reader subagent needs, the way `case step` does.

    Paths, not the text itself. The text is in `input`, and printing it here
    as well is what made the orchestrating session treat the reading as
    something to compose out of what it had seen rather than something to
    hand over — which is how the instruction that cost AMBQ's reading got
    written in the first place (T-015).
    """
    print(f"case: {case.id}  ticker: {case.ticker}  "
          f"quarter: {case.fiscal_quarter}")
    print(f"next_role: {package['role']}")
    print(f"system: {package['system']}")
    print(f"input: {package['input']}")
    print(f"schema: {package['schema']}")
    print(f"write_reply_to: {package['output']}")
    print(f"submit_with: hawkeye {submit_command} submit {case.id} "
          f"--file {package['output']}")


def cmd_guidance_queue(args: argparse.Namespace) -> int:
    """List the forward statements waiting to be read, or emit one package.

    One at a time, like the drop reviews: the caller spawns a fresh subagent
    per print, so nothing it read about the previous company can colour the
    next one's numbers.
    """
    cases = guidance_case.list_cases()
    if not cases:
        print("ガイダンスの読み取り待ちはありません。")
        return 0
    if args.case_id is None:
        print(f"読み取り待ち {len(cases)}件:")
        for c in cases:
            print(f"  {c.id}  {c.ticker:6s} {c.fiscal_quarter}")
        print(f"\n次: hawkeye guidance queue --case-id {cases[0].id}")
        return 0
    try:
        case = guidance_case.load_case(args.case_id)
    except FileNotFoundError:
        print(f"case not found: {args.case_id}", file=sys.stderr)
        return 1
    _print_reader_package(guidance_case.write_package(case), case, "guidance")
    return 0


def _guidance_recorded_ja(ticker: str, extraction) -> str:
    """何期ぶんの見通しが台帳に入り、何期が受け付けられなかったかを1行で言う。

    T-020 まで、1回の発表に複数の期の見通しがあっても記録は1期だけで、しかも
    残りが落ちたことはどこにも出なかった。操作している人間がその場で気づける
    唯一の場所がここなので、受け付けた期の名前と、受け付けなかった期の理由を
    そのまま出す。
    """
    if not extraction.readings:
        return (f"{ticker}: 会社の見通しは記録されませんでした"
                f"({extraction.reason})。")
    periods = "、".join(r.period or "期の記載なし" for r in extraction.readings)
    line = (f"{ticker}: 会社の見通しを{len(extraction.readings)}期ぶん"
            f"記録しました({periods})。")
    if extraction.refusals:
        line += (f" 受け付けなかった期が{len(extraction.refusals)}件"
                 f"あります({'、'.join(extraction.refusals)})。")
    return line


def cmd_guidance_submit(args: argparse.Namespace) -> int:
    """Validate one reading and attach it to the print row it belongs to.

    Prints the quarter's three-leg reading again afterwards, because the
    guidance leg is the only one that can still move at this point and a
    number that changed without being shown is a number the reader will act
    on the old value of.
    """
    try:
        case = guidance_case.load_case(args.case_id)
    except FileNotFoundError:
        print(f"case not found: {args.case_id}", file=sys.stderr)
        return 1
    try:
        extraction = parse_reply(guidance_case.load_reply(args.file),
                                 case.request(), model=args.reader or "")
    except (ValueError, OSError) as exc:
        print(f"読み取り結果を受け付けられません: {exc}", file=sys.stderr)
        return 1

    store = _stock_store()
    if guidance_case.attach(store, case, extraction) is None:
        print(f"{case.ticker}: 読み取り対象の決算行が入れ替わっているため"
              "反映しませんでした(実績値の訂正が間に入った可能性があります)。"
              "この銘柄はもう一度走査してください。", file=sys.stderr)
        return 1
    print(_guidance_recorded_ja(case.ticker, extraction))
    # Only now — the staged file is what makes a failed write retryable, the
    # same ordering the tribunal's case workspaces and the drop reviews use.
    # A refusal that is OURS rather than the company's keeps it (T-015).
    kept = _keep_staged(extraction.reason, guidance_failure_kind)
    if not kept:
        guidance_case.discard(case.id)

    row = store.active_print(case.stock_id, case.fiscal_quarter)
    consensus = (store.consensus(row.consensus_snapshot_id)
                 if row.consensus_snapshot_id else None)
    print(render_quality_ja(assess_earnings(row, consensus,
                                            HawkeyeConfig.from_env())))
    if kept:
        _report_kept_package(case.ticker, extraction.reason, "guidance",
                             case.id)
        return 1
    remaining = len(guidance_case.list_cases())
    print("\n次: " + (f"hawkeye guidance queue (残り {remaining}件)"
                      if remaining else "hawkeye case open ..."))
    return 0


def cmd_cause_queue(args: argparse.Namespace) -> int:
    """List the summaries waiting to be read for what they explain, or emit
    one package.

    One at a time, and one agent per print, for the same reason the guidance
    queue works that way: nothing the reader saw about the previous company
    may colour how it reads this one's sentence.
    """
    cases = cause_case.list_cases()
    if not cases:
        print("決算内容の理由の読み取り待ちはありません。")
        return 0
    if args.case_id is None:
        print(f"読み取り待ち {len(cases)}件:")
        for c in cases:
            print(f"  {c.id}  {c.ticker:6s} {c.fiscal_quarter}")
        print(f"\n次: hawkeye cause queue --case-id {cases[0].id}")
        return 0
    try:
        case = cause_case.load_case(args.case_id)
    except FileNotFoundError:
        print(f"case not found: {args.case_id}", file=sys.stderr)
        return 1
    _print_reader_package(cause_case.write_package(case), case, "cause")
    return 0


def cmd_cause_submit(args: argparse.Namespace) -> int:
    """Validate one reading of why the quarter came out where it did, and
    attach it to the print row it belongs to.

    Prints the quarter's three legs again afterwards so the reader sees what
    the tribunal will see. The SCORE is unchanged by this and always will be:
    the reading is the company's account of the figures, never a correction to
    them (invariant 1, invariant 6).
    """
    try:
        case = cause_case.load_case(args.case_id)
    except FileNotFoundError:
        print(f"case not found: {args.case_id}", file=sys.stderr)
        return 1
    try:
        extraction = parse_cause_reply(cause_case.load_reply(args.file),
                                       case.request(), model=args.reader or "")
    except (ValueError, OSError) as exc:
        print(f"読み取り結果を受け付けられません: {exc}", file=sys.stderr)
        return 1

    store = _stock_store()
    if cause_case.attach(store, case, extraction) is None:
        print(f"{case.ticker}: 読み取り対象の決算行が入れ替わっているため"
              "反映しませんでした(実績値の訂正が間に入った可能性があります)。"
              "この銘柄はもう一度走査してください。", file=sys.stderr)
        return 1
    # Only now — the staged file is what makes a failed write retryable, the
    # same ordering the tribunal's case workspaces and the drop reviews use.
    # A refusal that is OURS rather than the company's keeps it (T-015).
    kept = _keep_staged(extraction.reason, cause_failure_kind)
    if not kept:
        cause_case.discard(case.id)

    row = store.active_print(case.stock_id, case.fiscal_quarter)
    consensus = (store.consensus(row.consensus_snapshot_id)
                 if row.consensus_snapshot_id else None)
    print(render_quality_ja(assess_earnings(row, consensus,
                                            HawkeyeConfig.from_env())))
    if kept:
        _report_kept_package(case.ticker, extraction.reason, "cause",
                             case.id)
        return 1
    remaining = len(cause_case.list_cases())
    print("\n次: " + (f"hawkeye cause queue (残り {remaining}件)"
                      if remaining else "hawkeye rank"))
    return 0


def cmd_screened_list(args: argparse.Namespace) -> int:
    ledger = _ledger()
    rows = ledger.screened_candidates(scan_id=args.scan_id, stage=args.stage)
    if not rows:
        print("(該当する落選候補の記録なし)")
        return 0
    stage_label = {"enrichment_cap": "肉付け上限落ち", "gate_reject": "入口ゲート落ち",
                  "ranking_cutoff": "ランキング下位"}
    print(f"# 落選候補一覧 ({len(rows)}件)\n")
    print("| scan_id | 銘柄 | 段階 | スコア | 価格(基準日) | 理由 |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        price = f"${r.price:.2f} ({r.price_asof})" if r.price is not None else "-"
        print(f"| {r.scan_id} | {r.ticker} | {stage_label.get(r.stage.value, r.stage.value)} "
              f"| {r.score} | {price} | {r.reject_reason} |")
    return 0


def cmd_report_scan(args: argparse.Namespace) -> int:
    """The scan report the USER reads, before any case is opened (task 9).

    Built from the ledger rather than printed by the scan itself, because the
    moment it is needed is later: `/hawkeye-run` reads the company outlooks
    after the scan, and the user is asked to approve the shortlist after that.
    """
    ledger = _ledger()
    scan = ledger.scan(args.scan_id)
    if scan is None:
        print("走査の記録がありません" if args.scan_id is None else
              f"走査番号 {args.scan_id} の記録がありません", file=sys.stderr)
        return 1
    rows = ledger.screened_candidates(scan_id=scan["id"])
    print(render_scan_report_ja(scan, rows, top_n=args.top))
    # Always written, not only on request: the screen omits the names nobody
    # asked the feed about, so the file is the only complete record of a scan
    # the user can open. A flag they have to remember is a file that is
    # missing on the day it matters.
    path = (pathlib.Path(args.csv) if args.csv
            else reports_dir() / f"scan-{scan['id']}.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    # A BOM, for the same reason the check sheet carries one: Excel on
    # Japanese Windows reads a plain UTF-8 CSV as cp932 and turns every header
    # into mojibake.
    path.write_text(scan_report_csv(rows), encoding="utf-8-sig")
    print(f"\n全銘柄の表をCSVに保存しました: {path} ({len(rows)}行)")
    return 0


def cmd_drops_report(args: argparse.Namespace) -> int:
    """Score the funnel's rejects (docs/design/MASTER_OVERVIEW.ja.md §5.2(3)).

    Both ledger tables are read: `screened_candidates` holds the candidates
    dropped before the tribunal, `recommendations` the ones that reached it.
    Reviewing only the former would leave the tribunal unscored; reviewing
    only the latter is the blind spot §5.1 was written about.
    """
    config = HawkeyeConfig.from_env()
    ledger = _ledger()
    provider = _provider()

    tracked = [from_screened(c) for c in ledger.screened_candidates()
               if is_reviewable(c)]
    for row in ledger.list():
        rec = ledger.get(row["id"])
        if rec is not None:
            tracked.append(from_recommendation(rec))

    results, pending, censored = collect_checkpoints(
        tracked, provider, date.today(), config.drop_review_index_ticker,
        checkpoint=args.checkpoint)
    results = with_peer_baseline(results)

    # The aggregates always cover every stage — they are what unfreezes the
    # enrichment-cap settings (§5.2(6)). Only the per-name investigation
    # queue narrows, because reading an enrichment-cap drop one at a time
    # can't tell you what to change.
    if args.stage == "all":
        cohorts = None
    elif args.stage:
        cohorts = (args.stage,)
    else:
        cohorts = INVESTIGATION_COHORTS
    flagged = outliers(results, cohorts=cohorts)
    all_flagged = outliers(results, cohorts=None)
    suppressed = len(all_flagged) - len(flagged)
    reason = ("肉付け上限落ちは個別調査の対象外(落選理由が「サプライズ順で"
              "16位以下」しかなく、1銘柄ずつ読んでも打つ手が決まらないため。"
              "群の平均には計上済み)"
              if cohorts is INVESTIGATION_COHORTS
              else f"`--stage {args.stage}` で絞り込み中")

    print(render_drop_review_ja(
        checkpoint=args.checkpoint,
        horizon_days=CHECKPOINT_TRADING_DAYS[args.checkpoint],
        index_ticker=config.drop_review_index_ticker,
        results=results, pending=pending, censored=censored,
        cohort_table=attribute_by_cohort(results),
        gate_table=attribute_by_gate(results),
        flagged=flagged,
        min_samples=config.drop_review_min_samples_per_category,
        suppressed=suppressed, suppressed_reason=reason))
    return 0


# --- the review round (§5.2(3) [2][3][4]; driven by /hawkeye-review) --------

def _tracked_candidates(ledger) -> tuple[list, dict, dict]:
    """Everything the funnel dropped, plus the two lookups an investigation
    needs: what our own record held at decision time, and (for gate rejects)
    the catalyst date the news window should be anchored on."""
    tracked = []
    record_news: dict[str, list] = {}
    event_dates: dict[str, date] = {}
    for c in ledger.screened_candidates():
        if not is_reviewable(c):     # still held; nothing was judged yet
            continue
        t = from_screened(c)
        tracked.append(t)
        record_news[c.id] = list(c.news)
        event_dates[c.id] = c.event_date
    for row in ledger.list():
        rec = ledger.get(row["id"])
        if rec is None:
            continue
        tracked.append(from_recommendation(rec))
        record_news[rec.id] = list(rec.brief.news) if rec.brief else []
        if rec.brief is not None:
            event_dates[rec.id] = rec.brief.catalyst.event_date
    return tracked, record_news, event_dates


def cmd_drops_measure(args: argparse.Namespace) -> int:
    """Score every dropped candidate whose checkpoint has elapsed.

    Records all of them, not only the ones that moved: without the
    denominator, "3 names got away from us" cannot be read as a lot or a
    few. At T+10 the outliers are staged for investigation instead of being
    written straight away, because the row is written once, complete — the
    ledger has no UPDATE path by design.
    """
    config = HawkeyeConfig.from_env()
    ledger = _ledger()
    provider = _provider()
    checkpoint = args.checkpoint

    tracked, record_news, event_dates = _tracked_candidates(ledger)
    already = ledger.recorded_drop_review_keys(checkpoint)

    results, pending, censored = collect_checkpoints(
        tracked, provider, date.today(), config.drop_review_index_ticker,
        checkpoint=checkpoint)
    results = with_peer_baseline(results)
    plan = drop_cycle.plan(results, checkpoint, already_recorded=already)

    reviews = [to_drop_review(r, reviewer_model=args.reviewer or "")
               for r in plan.record_now]
    if reviews:
        ledger.record_drop_reviews(reviews)

    queued: list[str] = []
    for r in plan.investigate:
        subject_id = r.screened_candidate_id or r.rec_id or ""
        try:
            refetched = provider.news(
                r.ticker, limit=config.news_max_items,
                event_date=event_dates.get(subject_id),
                lead_days=config.news_lead_days)
        except Exception:
            refetched = []
        case = drop_case.open_case(
            r, record_at_decision=record_news.get(subject_id, []),
            refetched=refetched, reviewer_model=args.reviewer or "")
        drop_case.save_case(case)
        queued.append(case.id)

    total_before = len(ledger.drop_reviews()) - len(reviews)
    drop_cycle.save_round(drop_cycle.merge_round(
        drop_cycle.load_round(), checkpoint, plan,
        recorded_ids=[r.id for r in reviews], queued_case_ids=queued,
        pending=pending, censored=censored, total_before=total_before))

    print(f"{checkpoint}: 記録 {len(reviews)}件 / 調査待ちに追加 {len(queued)}件 "
          f"/ 観測期間未経過 {pending}件 / 記録済みのため対象外 "
          f"{plan.skipped_already_recorded}件 / 測定不能 {plan.unmeasurable}件")
    if queued:
        print("次: hawkeye drops queue")
    return 0


def cmd_drops_queue(args: argparse.Namespace) -> int:
    """Emit the investigation package for one queued case, or list the queue.

    One case at a time on purpose: the caller spawns a fresh subagent per
    name so the previous ticker's story cannot colour the next one's.
    """
    cases = drop_case.list_cases()
    if not cases:
        print("調査待ちはありません。")
        return 0
    if args.case_id is None:
        print(f"調査待ち {len(cases)}件:")
        for c in cases:
            m = c.measurement
            print(f"  {c.id}  {m.ticker:6s} {m.cohort:16s} "
                  f"z={m.z:+.2f}  判断日 {m.decision_date}")
        print(f"\n次: hawkeye drops queue --case-id {cases[0].id}")
        return 0
    try:
        case = drop_case.load_case(args.case_id)
    except FileNotFoundError:
        print(f"case not found: {args.case_id}", file=sys.stderr)
        return 1
    print(drop_case.render_input(case))
    print()
    print(f"submit_with: hawkeye drops submit {case.id} --file <調査結果.json>")
    return 0


def cmd_drops_submit(args: argparse.Namespace) -> int:
    """Merge one investigation into its measurement and record the row."""
    try:
        case = drop_case.load_case(args.case_id)
    except FileNotFoundError:
        print(f"case not found: {args.case_id}", file=sys.stderr)
        return 1
    try:
        review = drop_case.submit(case, drop_case.load_reply(args.file),
                                  reviewer_model=args.reviewer or "")
    except ValueError as exc:
        print(f"投稿された調査結果を受け付けられません: {exc}", file=sys.stderr)
        return 1

    ledger = _ledger()
    ledger.record_drop_reviews([review])
    # Only now — the staged file is what makes a failed ledger write
    # retryable (the same ordering as the tribunal's case workspaces).
    drop_case.discard(case.id)

    state = drop_cycle.load_round()
    state.recorded_ids.append(review.id)
    drop_cycle.save_round(state)

    label = review.miss_category.value if review.miss_category else "未分類"
    print(f"記録しました: {review.ticker} / 分類 {label} / {review.id}")
    remaining = len(drop_case.list_cases())
    print("次: " + (f"hawkeye drops queue (残り {remaining}件)"
                    if remaining else "hawkeye drops revise"))
    return 0


def cmd_drops_revise(args: argparse.Namespace) -> int:
    """Report the round and say whether any cause is ready for a revision.

    Always prints, threshold met or not: a review process that only speaks
    when it has a proposal is indistinguishable from one that stopped
    running. Drafting the revision itself is the caller's job — writing to
    strategy/ is a human-approved act, not something a CLI does on its own.
    """
    config = HawkeyeConfig.from_env()
    ledger = _ledger()
    state = drop_cycle.load_round()
    all_reviews = ledger.drop_reviews()
    min_samples = config.drop_review_min_samples_per_category

    this_round = [r for r in all_reviews if r.id in set(state.recorded_ids)]
    investigated = [r for r in this_round if r.miss_category is not None]
    measured = [r for r in this_round if r.miss_category is None]

    cohort_counts: dict[str, int] = {}
    for r in this_round:
        cohort_counts[r.cohort] = cohort_counts.get(r.cohort, 0) + 1

    ready = drop_cycle.ready_categories(all_reviews, min_samples)
    print(render_drop_cycle_ja(
        checkpoint=state.checkpoint or "t5/t10",
        measured=measured, investigated=investigated,
        cohort_counts=cohort_counts, censored=state.censored,
        pending=state.pending, skipped=state.skipped_already_recorded,
        remaining=drop_cycle.remaining_to_threshold(all_reviews, min_samples),
        ready=ready, min_samples=min_samples,
        previous_total=state.total_before))

    still_queued = len(drop_case.list_cases())
    if still_queued:
        print(f"\n⚠️ 調査待ちが {still_queued}件 残っています "
              "— 先に hawkeye drops queue を処理してください。")
        return 0
    if not args.keep_round:
        drop_cycle.clear_round()
    return 0


def cmd_docs_tribunal_roles(args: argparse.Namespace) -> int:
    """Regenerate (or verify) the readable copy of the tribunal's criteria.

    The prompts stay in `prompts.py`; this only renders them. `--check` is
    what a test and a reviewer use to catch a prompt edit that never made it
    into the document people actually read.
    """
    from pathlib import Path

    from hawkeye.reports.tribunal_roles import DOC_PATH, render_tribunal_roles_ja

    rendered = render_tribunal_roles_ja()
    target = Path(DOC_PATH)
    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current == rendered:
            print(f"{DOC_PATH}: ✅ prompts.py と一致")
            return 0
        print(f"{DOC_PATH}: ❌ prompts.py とずれています — "
              "`hawkeye docs tribunal-roles --write` で再生成してください",
              file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    print(f"{DOC_PATH}: 生成しました({len(rendered.splitlines())}行)")
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    config = HawkeyeConfig.from_env()
    official_horizon = config.phase0_benchmark_horizon_days
    horizon = args.horizon if args.horizon is not None else official_horizon
    exploratory = args.horizon is not None and args.horizon != official_horizon

    ledger = _ledger()
    provider = _provider()
    today = date.today()
    records = [ledger.get(row["id"]) for row in ledger.list()]
    records = [r for r in records if r is not None]
    samples, pending, censored = collect_samples(
        records, provider, today, horizon, source=args.source)

    print(f"# 📊 コホート・ベンチマーク(評価日から{horizon}営業日後のリターン、"
          f"対象コホート: {args.source})\n")
    if exploratory:
        print(f"⚠️ 探索用horizon(非公式) — Phase 0のキル基準判定には使えません。"
              f"公式値は{official_horizon}営業日です。\n")
    if not samples:
        print(f"対象データなし(評価から{horizon}営業日経過した記録がまだありません)")
        return 0
    stats = cohort_stats(samples)
    label = {"BUY": "🟢 BUY(推奨)", "TRIBUNAL_PASS": "⚪ 審理で見送り",
             "GATE_REJECT": "🚧 ゲートで却下"}
    print("| コホート | 件数 | 平均 | 中央値 | 勝率 | 打ち切り |")
    print("|---|---|---|---|---|---|")
    for cohort in ("BUY", "TRIBUNAL_PASS", "GATE_REJECT"):
        s = stats[cohort]
        c = censored[cohort]
        if s["n"] == 0:
            print(f"| {label[cohort]} | 0 | - | - | - | {c} |")
        else:
            print(f"| {label[cohort]} | {s['n']} | {s['mean']:+.2f}% | "
                  f"{s['median']:+.2f}% | {s['win_rate']:.0%} | {c} |")
    total_censored = sum(censored.values())
    if total_censored:
        print(f"\n⚠️ 打ち切り(株価取得失敗)が{total_censored}件あります。上場廃止・"
              f"銘柄コード変更・買収・API障害などで価格が取得できなかった銘柄は集計"
              f"から除外されており、往々にして最悪の結果を出した銘柄である可能性が"
              f"高いため、各群の平均は実態より良く見えている可能性があります"
              f"(生存者バイアス)。")
    buy, pas = stats["BUY"], stats["TRIBUNAL_PASS"]
    if buy["n"] > 0 and pas["n"] > 0:
        spread = buy["mean"] - pas["mean"]
        print(f"\nBUY − 見送り スプレッド: {spread:+.2f}%ポイント "
              f"({'✅ 絞り込みが価値を生んでいる方向' if spread > 0 else '⚠️ 絞り込みが価値を生んでいない — ロジック要見直し'})")
    if pending:
        print(f"\n(未経過: {pending}件 — まだ{horizon}営業日経過していません)")
    return 0


def cmd_review_passes(args: argparse.Namespace) -> int:
    """Individual postmortem: which specific PASS/DECLINE calls look, in
    hindsight, like mistakes — vs. `benchmark`'s aggregate cohort view."""
    ledger = _ledger()
    provider = _provider()
    today = date.today()
    flagged: list[dict] = []
    pending = 0
    censored = 0
    review_statuses = {RecommendationStatus.SYSTEM_PASS.value,
                       RecommendationStatus.DECLINED.value}
    min_wait_days = min_calendar_days_for_trading_days(args.horizon)
    for row in ledger.list():
        if row["status"] not in review_statuses:
            continue
        rec = ledger.get(row["id"])
        if rec is None:
            continue
        eval_day = utc_date(rec.created_at)
        if (today - eval_day).days < min_wait_days:
            pending += 1
            continue
        try:
            bars = provider.daily_history(rec.ticker, days=400)
        except Exception:
            censored += 1
            continue
        ret = forward_return(bars, eval_day, args.horizon)
        if ret is None:
            censored += 1
            continue
        if abs(ret) >= args.threshold:
            flagged.append({"rec": rec, "return_pct": round(ret, 2),
                            "status": row["status"]})

    flagged.sort(key=lambda d: -abs(d["return_pct"]))
    print(f"# 🔍 見送り案件の事後レビュー(評価から{args.horizon}営業日後、"
         f"±{args.threshold:.0f}%以上動いた銘柄)\n")
    if not flagged:
        print(f"該当なし(閾値{args.threshold:.0f}%を超えて動いた見送り銘柄はありません)")
        if censored:
            print(f"⚠️ 打ち切り(株価取得失敗): {censored}件 — "
                 f"上場廃止・買収・API障害などの可能性")
        if pending:
            print(f"(未経過: {pending}件 — まだ{args.horizon}営業日経過していません)")
        return 0
    for item in flagged:
        rec, ret = item["rec"], item["return_pct"]
        arrow = "📈" if ret > 0 else "📉"
        tag = ("見送り(判断ミスの可能性 — 上昇)" if ret > 0
              else "見送り(結果的に正しかった可能性 — 下落)")
        print(f"## {arrow} {rec.ticker}  {ret:+.1f}%  [{tag}]")
        print(f"- 提案ID: {rec.id}  評価日時: {fmt_jst(rec.created_at)}")
        print(f"- 理由: {reason_snippet(rec, item['status'])}")
        print(f"- 詳細: hawkeye show {rec.id}")
        print()
    print("注意: 上昇していても「判断ミス」とは限りません。見送り後に新しい好材料が"
         "出た可能性もあります。`hawkeye show` で当時の反証内容を確認した上で、"
         "プロセス(ゲート閾値・反証の甘さ等)に起因するのか、単なる後知恵バイアスかを"
         "判断してください。")
    if censored:
        print(f"\n⚠️ 打ち切り(株価取得失敗): {censored}件 — "
             f"上場廃止・買収・API障害などの可能性")
    if pending:
        print(f"(未経過: {pending}件 — まだ{args.horizon}営業日経過していません)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    ledger = _ledger()
    rec = ledger.get(args.rec_id)
    if rec is None:
        print(f"recommendation not found: {args.rec_id}", file=sys.stderr)
        return 1
    print(render_recommendation_ja(rec))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    ledger = _ledger()
    status = RecommendationStatus(args.status) if args.status else None
    rows = ledger.list(status)
    if not rows:
        print("(記録なし)")
        return 0
    for r in rows:
        print(f"{r['id']}  {r['ticker']:<6}  {r['status']:<12}  "
              f"{fmt_jst(r['created_at'])}")
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    ledger = _ledger()
    if ledger.get(args.rec_id) is None:
        print(f"recommendation not found: {args.rec_id}", file=sys.stderr)
        return 1
    if ledger.status(args.rec_id) != RecommendationStatus.PROPOSED:
        print(f"warning: status is {ledger.status(args.rec_id).value}, not proposed",
              file=sys.stderr)
    ledger.record_decision(args.rec_id, approved=args.yes, note=args.note or "")
    print("記録しました: " + ("承認(Yes)— 発注はご自身で実行してください"
                          if args.yes else "見送り(No)"))
    return 0


def cmd_record_entry(args: argparse.Namespace) -> int:
    ledger = _ledger()
    if ledger.get(args.rec_id) is None:
        print(f"recommendation not found: {args.rec_id}", file=sys.stderr)
        return 1
    ledger.record_entry(args.rec_id, price=args.price, shares=args.shares,
                        trade_date=date.fromisoformat(args.date))
    print(f"エントリー記録: {args.shares}株 @ ${args.price} ({args.date})")
    return 0


def cmd_positions(args: argparse.Namespace) -> int:
    ledger = _ledger()
    positions = ledger.open_positions()
    if not positions:
        print("(保有ポジションなし)")
        return 0
    for rec in positions:
        entry = ledger.entry(rec.id) or {}
        stop = rec.plan.stop_price if rec.plan else None
        print(f"{rec.id}  {rec.ticker:<6}  entry=${entry.get('price', '?')} "
              f"({entry.get('date', '?')})  stop=${stop}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    ledger = _ledger()
    positions = ledger.open_positions()
    if not positions:
        print("(保有ポジションなし — チェック対象なし)")
        return 0
    price_overrides = dict(
        kv.split("=") for kv in (args.price or [])) if args.price else {}
    provider = _provider()
    today = date.today()
    any_signal = False
    for rec in positions:
        entry = ledger.entry(rec.id)
        if entry is None:
            continue
        if rec.ticker in price_overrides:
            current = float(price_overrides[rec.ticker])
        else:
            bars = provider.daily_history(rec.ticker, days=5)
            if not bars:
                print(f"{rec.ticker}: 価格取得失敗 — --price {rec.ticker}=XX.XX で指定してください")
                continue
            current = bars[-1].close
        resolved = frozenset(ledger.claim_resolutions(rec.id).keys())
        signals = check_position(rec, current_price=current, today=today,
                                 entry_date=date.fromisoformat(entry["date"]),
                                 resolved_claim_ids=resolved)
        for sig in signals:
            ledger.record_signal(rec.id, {"kind": sig.kind,
                                          "severity": sig.severity,
                                          "message": sig.message,
                                          "price": current})
        print(render_signals_ja(rec.ticker, signals))
        print()
        any_signal = any_signal or bool(signals)
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    ledger = _ledger()
    if ledger.get(args.rec_id) is None:
        print(f"recommendation not found: {args.rec_id}", file=sys.stderr)
        return 1
    ledger.record_exit(args.rec_id, price=args.price,
                       trade_date=date.fromisoformat(args.date),
                       note=args.note or "")
    print(f"クローズ記録: @ ${args.price} ({args.date})。"
          f"次: 未解決のクレームを resolve-claim で解決し、outcome を実行してください。")
    return 0


def cmd_resolve_claim(args: argparse.Namespace) -> int:
    ledger = _ledger()
    rec = ledger.get(args.rec_id)
    if rec is None or rec.thesis is None:
        print(f"recommendation/thesis not found: {args.rec_id}", file=sys.stderr)
        return 1
    claim_ids = {c.id for c in rec.thesis.claims}
    if args.claim_id not in claim_ids:
        print(f"claim not found. valid ids: {sorted(claim_ids)}", file=sys.stderr)
        return 1
    ledger.resolve_claim(args.rec_id, args.claim_id,
                         outcome=args.true_, note=args.note or "")
    print(f"クレーム解決を記録: {args.claim_id} -> {'TRUE' if args.true_ else 'FALSE'}")
    return 0


def cmd_claims(args: argparse.Namespace) -> int:
    ledger = _ledger()
    rec = ledger.get(args.rec_id)
    if rec is None or rec.thesis is None:
        print(f"recommendation/thesis not found: {args.rec_id}", file=sys.stderr)
        return 1
    resolutions = ledger.claim_resolutions(args.rec_id)
    for c in rec.thesis.claims:
        state = "未解決"
        if c.id in resolutions:
            outcome, note = resolutions[c.id]
            state = ("TRUE" if outcome else "FALSE") + (f" ({note})" if note else "")
        print(f"{c.id}  p={c.probability:.0%}  {c.horizon_days}日  [{state}]")
        print(f"    {c.statement}")
    return 0


def cmd_outcome(args: argparse.Namespace) -> int:
    config = HawkeyeConfig.from_env()
    ledger = _ledger()
    rec = ledger.get(args.rec_id)
    if rec is None:
        print(f"recommendation not found: {args.rec_id}", file=sys.stderr)
        return 1
    entry, exit_ = ledger.entry(args.rec_id), ledger.exit(args.rec_id)
    if entry is None or exit_ is None:
        print("entry/exit trades not recorded yet", file=sys.stderr)
        return 1
    resolutions = ledger.claim_resolutions(args.rec_id)
    resolved_pairs = []
    if rec.thesis is not None:
        for c in rec.thesis.claims:
            if c.id in resolutions:
                resolved_pairs.append((c.probability, resolutions[c.id][0]))
    pnl_pct = (exit_["price"] / entry["price"] - 1.0) * 100.0
    entry_d, exit_d = date.fromisoformat(entry["date"]), date.fromisoformat(exit_["date"])
    accuracy = thesis_accuracy(resolved_pairs)
    outcome = Outcome(
        recommendation_id=args.rec_id,
        entry_price=entry["price"], exit_price=exit_["price"],
        entry_date=entry_d, exit_date=exit_d,
        pnl_pct=round(pnl_pct, 2),
        holding_days=(exit_d - entry_d).days,
        thesis_accuracy=accuracy,
        brier=brier_score(resolved_pairs),
        quadrant=classify_outcome(pnl_pct, accuracy,
                                  config.thesis_accuracy_threshold),
        notes=args.note or "")
    ledger.record_outcome(outcome)
    quadrant_ja = {
        "skill_win": "実力による勝ち(仮説的中・利益)",
        "lucky_win": "運による勝ち(仮説外れ・利益)⚠️ プロセス要点検",
        "unlucky_loss": "運による負け(仮説的中・損失)",
        "deserved_loss": "プロセスの負け(仮説外れ・損失)⚠️ 要因分析必須",
    }
    print(f"損益: {pnl_pct:+.2f}%  保有: {outcome.holding_days}日")
    print(f"仮説的中率: {accuracy:.0%}" if accuracy is not None
          else "仮説的中率: 未解決クレームあり(resolve-claim を先に)")
    if outcome.brier is not None:
        print(f"Brierスコア: {outcome.brier:.3f} (0=完璧, 0.25=コイン投げ)")
    if outcome.quadrant is not None:
        print(f"帰属: {quadrant_ja[outcome.quadrant.value]}")
    return 0


def cmd_calibration(args: argparse.Namespace) -> int:
    ledger = _ledger()
    pairs = ledger.all_resolved_claims()
    outcomes = ledger.all_outcomes()
    print(f"解決済みクレーム数: {len(pairs)}")
    b = brier_score(pairs)
    if b is not None:
        print(f"全体Brierスコア: {b:.3f} (0=完璧, 0.25=コイン投げ)")
    print("\nキャリブレーション(確率帯ごとの実際の的中率):")
    for row in calibration_table(pairs):
        stated = f"{row['avg_stated']:.0%}" if row["avg_stated"] is not None else "-"
        actual = f"{row['freq_true']:.0%}" if row["freq_true"] is not None else "-"
        print(f"  {row['band']}: n={row['n']}  申告平均={stated}  実際={actual}")
    if outcomes:
        print("\nクローズ済みトレードの帰属:")
        counts: dict[str, int] = {}
        for o in outcomes:
            if o.quadrant:
                counts[o.quadrant.value] = counts.get(o.quadrant.value, 0) + 1
        for k, v in sorted(counts.items()):
            print(f"  {k}: {v}")
        avg = sum(o.pnl_pct for o in outcomes) / len(outcomes)
        print(f"  平均損益: {avg:+.2f}% ({len(outcomes)}トレード)")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    ok = _ledger().verify_chain()
    print("台帳ハッシュチェーン: " + ("✅ 整合" if ok else "❌ 改ざん検知"))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hawkeye",
        description="Hawkeye — adversarial-verification investment decision system")
    sub = p.add_subparsers(dest="command", required=True)

    ev = sub.add_parser("evaluate", help="run one candidate through the tribunal")
    ev.add_argument("ticker")
    ev.add_argument("--catalyst", required=True,
                    choices=[c.value for c in CatalystType])
    ev.add_argument("--description", required=True,
                    help="what happened (facts only)")
    ev.add_argument("--event-date", required=True, help="YYYY-MM-DD")
    ev.add_argument("--source", default="")
    ev.add_argument("--notes", default="")
    ev.add_argument("--nav", type=float, default=100_000.0)
    ev.add_argument("--price", type=float, default=None,
                    help="override: current price")
    ev.add_argument("--market-cap", type=float, default=None,
                    help="override: market cap USD")
    ev.add_argument("--adv", type=float, default=None,
                    help="override: 20d avg dollar volume USD")
    ev.add_argument("--atr-pct", type=float, default=None)
    ev.add_argument("--gap-pct", type=float, default=None)
    ev.add_argument("--days-since-event", type=int, default=None)
    ev.add_argument("--eps-surprise-pct", type=float, default=None,
                    help="structured EPS surprise %% (if known)")
    ev.add_argument("--revenue-surprise-pct", type=float, default=None,
                    help="structured revenue surprise %% (if known)")
    ev.set_defaults(func=cmd_evaluate)

    sc = sub.add_parser("scout", help="scan earnings surprises for candidates "
                                       "(gates only — no score, no ledger "
                                       "write; follow with `rank`)")
    sc.add_argument("--days", type=int, default=None,
                    help="scan window in days (default: config)")
    sc.add_argument("--monitor-csv", default=None, metavar="PATH",
                    help="save the inspection table (取得データ点検表) as CSV")
    sc.set_defaults(func=cmd_scout)

    rk = sub.add_parser("rank", help="score and record the pending scan "
                                      "(run once `hawkeye guidance queue` "
                                      "is empty)")
    rk.add_argument("--evaluate", type=int, default=0, metavar="N",
                    help="run the tribunal on the top N candidates (API mode)")
    rk.add_argument("--open-cases", type=int, default=0, metavar="N",
                    help="open session-mode cases for the top N candidates "
                         "(no API key; driven by /hawkeye-run)")
    rk.add_argument("--nav", type=float, default=100_000.0)
    rk.add_argument("--allow-unread-guidance", action="store_true",
                    help="rank even with guidance readings still queued "
                         "(recorded on the scan row; use only when a reading "
                         "cannot be made to pass)")
    rk.set_defaults(func=cmd_rank)

    cn = sub.add_parser("consensus",
                        help="pre-register consensus before earnings prints")
    cn_sub = cn.add_subparsers(dest="consensus_cmd", required=True)
    cnc = cn_sub.add_parser(
        "capture",
        help="snapshot the consensus for prints due in the next business days")
    cnc.add_argument("--days", type=int, default=None,
                     help="business days ahead to cover (default: config)")
    cnc.add_argument("--limit", type=int, default=None,
                     help="cap the number of names (each costs ~1s of Yahoo)")
    cnc.add_argument("--dry-run", action="store_true",
                     help="list the names without recording anything")
    cnc.set_defaults(func=cmd_consensus_capture)

    cnd = cn_sub.add_parser(
        "drift",
        help="did the pre-registered consensus match what the feed says after "
             "the print? (a measurement; writes nothing)")
    cnd.add_argument("--limit", type=int, default=None,
                     help="cap the number of names (each costs one request)")
    cnd.add_argument("--only-moved", action="store_true",
                     help="print only the names whose consensus changed")
    cnd.set_defaults(func=cmd_consensus_drift)

    stk = sub.add_parser("stocks", help="the stock master (CIK-keyed)")
    stk_sub = stk.add_subparsers(dest="stocks_cmd", required=True)
    stks = stk_sub.add_parser(
        "show", help="one company: prints, frozen consensus, past decisions")
    stks.add_argument("ticker")
    stks.set_defaults(func=cmd_stocks_show)
    stkl = stk_sub.add_parser("list", help="every stock on record")
    stkl.set_defaults(func=cmd_stocks_list)
    stkr = stk_sub.add_parser(
        "rebuild", help="rebuild the review projection from the ledger")
    stkr.set_defaults(func=cmd_stocks_rebuild)
    stkp = stk_sub.add_parser(
        "prune-revisions",
        help="delete print rows RETIRED by a correction. Previews unless "
             "--apply. The row that stands is never touched — but the "
             "retired one is the only record of the figure a past ranking "
             "was made on, so this is deliberately a separate step.")
    stkp.add_argument("--ticker", default=None, help="only this company")
    stkp.add_argument("--apply", action="store_true",
                      help="actually delete (default is a preview)")
    stkp.set_defaults(func=cmd_stocks_prune_revisions)

    gd = sub.add_parser("guidance",
                        help="read the company's own outlook out of the "
                             "print's prose (task 8.7 layer 2)")
    gd_sub = gd.add_subparsers(dest="guidance_command", required=True)
    gdq = gd_sub.add_parser("queue",
                            help="list what is waiting, or emit one package")
    gdq.add_argument("--case-id", default=None,
                     help="emit this case's package instead of the list")
    gdq.set_defaults(func=cmd_guidance_queue)
    gds = gd_sub.add_parser("submit", help="attach one reading to its print")
    gds.add_argument("case_id")
    gds.add_argument("--file", required=True, help="the agent's JSON reply")
    gds.add_argument("--reader", default=None,
                     help="which model read it (recorded on the row)")
    gds.set_defaults(func=cmd_guidance_submit)

    cz = sub.add_parser("cause",
                        help="read what the company said explains the quarter "
                             "it just reported, out of the same prose (T-003)")
    cz_sub = cz.add_subparsers(dest="cause_command", required=True)
    czq = cz_sub.add_parser("queue",
                            help="list what is waiting, or emit one package")
    czq.add_argument("--case-id", default=None,
                     help="emit this case's package instead of the list")
    czq.set_defaults(func=cmd_cause_queue)
    czs = cz_sub.add_parser("submit", help="attach one reading to its print")
    czs.add_argument("case_id")
    czs.add_argument("--file", required=True, help="the agent's JSON reply")
    czs.add_argument("--reader", default=None,
                     help="which model read it (recorded on the row)")
    czs.set_defaults(func=cmd_cause_submit)
    czr = cz_sub.add_parser(
        "source",
        help="show the release excerpt the reader would be given, and every "
             "block the extractor returned that the release does not contain")
    czr.add_argument("ticker")
    czr.add_argument("--model", default="",
                     help="which extractor to ask (default: the one the scan "
                          "uses). Comparing two models on the same release, "
                          "and seeing what a refused one actually says, are "
                          "the reasons this exists")
    czr.set_defaults(func=cmd_cause_source)

    rep = sub.add_parser("report",
                         help="reports written for the user to read")
    rep_sub = rep.add_subparsers(dest="report_command", required=True)
    rps = rep_sub.add_parser(
        "scan", help="one scan, for the user: the shortlist, what earned each "
                     "score, and what happened to every other name")
    rps.add_argument("--scan-id", type=int, default=None,
                     help="which scan (default: the most recent one)")
    rps.add_argument("--top", type=int, default=3,
                     help="how many names the tribunal will take (default 3)")
    rps.add_argument("--csv", default=None,
                     help="where to write the all-names CSV "
                          "(default: var/reports/scan-<id>.csv)")
    rps.set_defaults(func=cmd_report_scan)

    sd = sub.add_parser("screened",
                        help="review candidates the scout funnel dropped "
                             "(docs/design/MASTER_OVERVIEW.ja.md §5.1)")
    sd_sub = sd.add_subparsers(dest="screened_command", required=True)
    sdl = sd_sub.add_parser("list", help="list dropped candidates")
    sdl.add_argument("--scan-id", type=int, default=None,
                     help="restrict to one scan (see `hawkeye scout` output)")
    sdl.add_argument("--stage",
                     choices=[s.value for s in ScreenedCandidateStage],
                     default=None, help="restrict to one funnel stage")
    sdl.set_defaults(func=cmd_screened_list)

    dr = sub.add_parser("drops",
                        help="score the candidates the funnel dropped "
                             "(docs/design/MASTER_OVERVIEW.ja.md §5.2(3))")
    dr_sub = dr.add_subparsers(dest="drops_command", required=True)
    drr = dr_sub.add_parser(
        "report",
        help="alpha/z per funnel stage and per gate at a fixed checkpoint")
    # No free-form horizon: the checkpoints are pre-registered (T+5/T+10) so
    # a disappointing result can't be re-run at whatever horizon flatters it.
    drr.add_argument("--checkpoint", choices=sorted(CHECKPOINT_TRADING_DAYS),
                     default="t5",
                     help="T+5 (default) or T+10 trading days after the drop")
    drr.add_argument("--stage", choices=sorted(COHORTS) + ["all"],
                     default=None,
                     help="restrict the per-name investigation queue to one "
                          "funnel stage ('all' includes enrichment-cap drops, "
                          "which the default leaves out). Aggregates always "
                          "cover every stage regardless.")
    drr.set_defaults(func=cmd_drops_report)

    # The review round (§5.2(3) [2][3][4]). Driven by /hawkeye-review, which
    # runs in its OWN session: the round reads a lot of history and produces
    # a lot of prose, and mixing it into /hawkeye-run would make every daily
    # candidate cycle pay for it.
    drm = dr_sub.add_parser(
        "measure",
        help="score every elapsed drop at one checkpoint and record it; "
             "at t10, stage the outliers for investigation")
    drm.add_argument("--checkpoint", choices=sorted(CHECKPOINT_TRADING_DAYS),
                     required=True,
                     help="t5 (measure and file) or t10 (measure, then queue "
                          "the outliers for a per-name investigation)")
    drm.add_argument("--reviewer", default="",
                     help="engine label recorded with the rows")
    drm.set_defaults(func=cmd_drops_measure)

    drq = dr_sub.add_parser(
        "queue", help="list the investigation queue, or emit one case's package")
    drq.add_argument("--case-id", default=None,
                     help="emit this case's investigation package "
                          "(omit to list the queue)")
    drq.set_defaults(func=cmd_drops_queue)

    drs = dr_sub.add_parser(
        "submit", help="merge one investigation into its measurement and record")
    drs.add_argument("case_id")
    drs.add_argument("--file", required=True,
                     help="JSON file holding the investigation reply")
    drs.add_argument("--reviewer", default="")
    drs.set_defaults(func=cmd_drops_submit)

    drv = dr_sub.add_parser(
        "revise",
        help="report the round and say whether any cause reached the "
             "threshold for drafting a revision (prints either way)")
    drv.add_argument("--keep-round", action="store_true",
                     help="do not clear the round state (for re-printing)")
    drv.set_defaults(func=cmd_drops_revise)

    # Generated strategy docs. The prompts stay in prompts.py (a prompt rule
    # and the code enforcing it only mean something together, and both engines
    # must read the same constant) — this only renders a readable copy.
    dc = sub.add_parser("docs", help="generate strategy documents from code")
    dc_sub = dc.add_subparsers(dest="docs_command", required=True)
    dtr = dc_sub.add_parser(
        "tribunal-roles",
        help="render strategy/TRIBUNAL_ROLES.ja.md from prompts.py")
    dtr.add_argument("--check", action="store_true",
                     help="verify the committed document matches prompts.py "
                          "instead of rewriting it (exit 1 on drift)")
    dtr.add_argument("--write", action="store_true",
                     help="write the document (the default; accepted so the "
                          "command printed in the document's own header works)")
    dtr.set_defaults(func=cmd_docs_tribunal_roles)

    # session-mode case workflow (LLM driven by Claude Code, no API key)
    ca_p = sub.add_parser("case",
                          help="stepwise tribunal for session mode (no API key)")
    ca_sub = ca_p.add_subparsers(dest="case_command", required=True)

    co = ca_sub.add_parser("open", help="run gates and open a case")
    co.add_argument("ticker")
    co.add_argument("--catalyst", default=CatalystType.EARNINGS_BEAT.value,
                    choices=[c.value for c in CatalystType])
    # Free text and a date, UNLESS --from-earnings supplies both from the
    # three-leg reading of the actual print (§5.3). Judging a stock a person
    # named on hand-typed prose would be a second, weaker standard of
    # evidence living beside the funnel's.
    co.add_argument("--description", default="")
    co.add_argument("--event-date", default=None, help="YYYY-MM-DD")
    co.add_argument("--from-earnings", action="store_true",
                    help="judge this ticker's latest print on the three legs "
                         "(EPS/revenue/guidance) and use that as the catalyst")
    co.add_argument("--source", default="")
    co.add_argument("--notes", default="")
    co.add_argument("--nav", type=float, default=100_000.0)
    co.add_argument("--price", type=float, default=None)
    co.add_argument("--market-cap", type=float, default=None)
    co.add_argument("--adv", type=float, default=None)
    co.add_argument("--atr-pct", type=float, default=None)
    co.add_argument("--gap-pct", type=float, default=None)
    co.add_argument("--days-since-event", type=int, default=None)
    co.add_argument("--eps-surprise-pct", type=float, default=None)
    co.add_argument("--revenue-surprise-pct", type=float, default=None)
    co.set_defaults(func=cmd_case_open)

    cs = ca_sub.add_parser("step", help="emit the next role's prompt package")
    cs.add_argument("case_id")
    cs.set_defaults(func=cmd_case_step)

    cu = ca_sub.add_parser("submit", help="submit a role's JSON output")
    cu.add_argument("case_id")
    cu.add_argument("--file", required=True)
    cu.set_defaults(func=cmd_case_submit)

    cl_ = ca_sub.add_parser("list", help="list cases and their state")
    cl_.set_defaults(func=cmd_case_list)

    bm = sub.add_parser("benchmark",
                        help="forward returns: BUY vs PASS vs gate-reject cohorts")
    bm.add_argument("--horizon", type=int, default=None,
                    help="trading days after evaluation (default: the "
                         "pinned official Phase-0 value, "
                         "config.phase0_benchmark_horizon_days=30. An "
                         "explicit value here is labeled exploratory/"
                         "non-authoritative in the output — it is not the "
                         "Phase-0 kill-criterion measurement)")
    bm.add_argument("--source", choices=["scout", "manual", "all"],
                    default="scout",
                    help="cohort source filter (default: scout-only. Per "
                         "strategy/ROADMAP.md, manually-picked `evaluate` "
                         "candidates are a separate cohort and must not "
                         "enter viability stats)")
    bm.set_defaults(func=cmd_benchmark)

    rp = sub.add_parser("review-passes",
                        help="flag individual PASS/DECLINE calls that moved "
                             "a lot afterward (postmortem, not aggregate stats)")
    rp.add_argument("--horizon", type=int, default=30,
                    help="trading days after evaluation (default 30)")
    rp.add_argument("--threshold", type=float, default=15.0,
                    help="flag moves at or beyond this %% magnitude (default 15)")
    rp.set_defaults(func=cmd_review_passes)

    sh = sub.add_parser("show", help="re-render a stored recommendation")
    sh.add_argument("rec_id")
    sh.set_defaults(func=cmd_show)

    ls = sub.add_parser("list", help="list recommendations")
    ls.add_argument("--status", default=None,
                    choices=[s.value for s in RecommendationStatus])
    ls.set_defaults(func=cmd_list)

    de = sub.add_parser("decide", help="record the user's Yes/No")
    de.add_argument("rec_id")
    g = de.add_mutually_exclusive_group(required=True)
    g.add_argument("--yes", action="store_true")
    g.add_argument("--no", dest="yes", action="store_false")
    de.add_argument("--note", default="")
    de.set_defaults(func=cmd_decide)

    re_ = sub.add_parser("record-entry", help="record the executed entry fill")
    re_.add_argument("rec_id")
    re_.add_argument("--price", type=float, required=True)
    re_.add_argument("--shares", type=int, required=True)
    re_.add_argument("--date", required=True, help="YYYY-MM-DD")
    re_.set_defaults(func=cmd_record_entry)

    po = sub.add_parser("positions", help="list open positions")
    po.set_defaults(func=cmd_positions)

    ch = sub.add_parser("check", help="sentinel sweep of open positions")
    ch.add_argument("--price", action="append", metavar="TICKER=PRICE",
                    help="manual price override (repeatable)")
    ch.set_defaults(func=cmd_check)

    cl = sub.add_parser("close", help="record the executed exit fill")
    cl.add_argument("rec_id")
    cl.add_argument("--price", type=float, required=True)
    cl.add_argument("--date", required=True, help="YYYY-MM-DD")
    cl.add_argument("--note", default="")
    cl.set_defaults(func=cmd_close)

    cm = sub.add_parser("claims", help="list a recommendation's claims")
    cm.add_argument("rec_id")
    cm.set_defaults(func=cmd_claims)

    rc = sub.add_parser("resolve-claim", help="resolve a claim TRUE/FALSE")
    rc.add_argument("rec_id")
    rc.add_argument("claim_id")
    g = rc.add_mutually_exclusive_group(required=True)
    g.add_argument("--true", dest="true_", action="store_true")
    g.add_argument("--false", dest="true_", action="store_false")
    rc.add_argument("--note", default="")
    rc.set_defaults(func=cmd_resolve_claim)

    oc = sub.add_parser("outcome", help="compute attribution for a closed trade")
    oc.add_argument("rec_id")
    oc.add_argument("--note", default="")
    oc.set_defaults(func=cmd_outcome)

    ca = sub.add_parser("calibration", help="book-level calibration stats")
    ca.set_defaults(func=cmd_calibration)

    ve = sub.add_parser("verify", help="verify the ledger hash chain")
    ve.set_defaults(func=cmd_verify)

    return p


def main(argv: list[str] | None = None) -> int:
    load_local_env()
    # Windows consoles default stdout/stderr to the system codepage (cp932
    # for Japanese locales), which can't encode em dashes or emoji used
    # throughout this CLI's output and help text — the same bug class fixed
    # for file I/O in commit 01152f2, here for the console streams instead.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    # Every command that reads the earnings calendar fails the same way when
    # the feed is down, and none of them can do anything useful without it.
    # Reported as a plain message rather than a traceback, and never as a
    # result: a command that could not look has not found nothing.
    try:
        return args.func(args)
    except CalendarUnavailable as exc:
        print(f"決算カレンダーを読めませんでした: {exc}", file=sys.stderr)
        print("Finnhub の障害か、キーの権限切れの可能性があります。"
              "時間をおいて再実行してください。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
