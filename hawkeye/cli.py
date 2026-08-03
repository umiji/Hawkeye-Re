"""Hawkeye CLI — the manual operating surface for the MVP.

Daily rhythm (docs/USER_GUIDE.ja.md):
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
import sys
from datetime import date

from hawkeye.config import HawkeyeConfig
from hawkeye.paths import db_path
from hawkeye.envfile import load_local_env
from hawkeye.contracts.models import (
    Catalyst,
    CatalystType,
    DecisionType,
    Outcome,
    Recommendation,
    RecommendationStatus,
    ScreenedCandidateStage,
    utc_date,
)
from hawkeye.ledger.scoring import (
    brier_score,
    calibration_table,
    classify_outcome,
    thesis_accuracy,
)
from hawkeye.ledger.store import Ledger
from hawkeye.marketdata.finnhub import CompositeProvider, FinnhubProvider
from hawkeye.marketdata.snapshot import build_brief
from hawkeye.marketdata.yahoo import YahooProvider
from hawkeye.marketdata.yahoo_earnings import YahooEarningsSource
from hawkeye.reports.render_ja import (
    fmt_jst,
    render_drop_cycle_ja,
    render_drop_review_ja,
    render_recommendation_ja,
    render_scout_ja,
    render_signals_ja,
)
from hawkeye.scout import drop_case, drop_cycle
from hawkeye.scout.drop_review import (
    CHECKPOINT_TRADING_DAYS,
    COHORTS,
    INVESTIGATION_COHORTS,
    attribute_by_cohort,
    attribute_by_gate,
    collect_checkpoints,
    from_recommendation,
    from_screened,
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
from hawkeye.marketdata.consensus import YahooConsensusSource
from hawkeye.marketdata.edgar import EdgarDirectory
from hawkeye.marketdata.edgar_facts import EdgarFacts
from hawkeye.paths import releases_dir
from hawkeye.reports.quality_ja import (
    render_quality_ja,
    render_release_requests_ja,
    render_stock_history_ja,
)
from hawkeye.scout.release import DirectoryReleaseReader
from hawkeye.scout.single import judge_ticker
from hawkeye.scout.prereg import (
    business_days_ahead,
    capture_consensus,
    report_line,
    upcoming_prints,
    warn_if_nothing_captured,
)
from hawkeye.scout.scout import (
    build_screened_candidates,
    run_scout,
    scan_window,
)
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
    print(f"\n(記録済み: {rec.id} / status={status.value} / DB={db_path()})")
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


def _judged_earnings(args: argparse.Namespace):
    """The three-leg reading of a named stock's latest quarter, or None.

    Same path the funnel uses — same second source, same both-sources-agree
    rule, same pinned consensus — so a stock a person named arrives at the
    tribunal on exactly the evidence a discovered one would.
    """
    finnhub = FinnhubProvider()
    if not finnhub.available:
        print("--from-earnings には FINNHUB_API_KEY が必要です", file=sys.stderr)
        return None
    numbers = YahooEarningsSource()
    return judge_ticker(
        args.ticker, finnhub, HawkeyeConfig.from_env(),
        report_date=(date.fromisoformat(args.event_date)
                     if args.event_date else None),
        numbers_source=numbers if numbers.available else None,
        stock_store=_stock_store(), directory=EdgarDirectory(),
        consensus_source=YahooConsensusSource() if numbers.available else None,
        facts=EdgarFacts(),
        release_reader=DirectoryReleaseReader(releases_dir()))


def cmd_case_open(args: argparse.Namespace) -> int:
    config = HawkeyeConfig.from_env()
    judged = _judged_earnings(args) if args.from_earnings else None
    if args.from_earnings:
        if judged is None:
            print(f"{args.ticker}: 決算カレンダーに実績のある行が見つかりません"
                  f"(--event-date で発表日を指定してください)", file=sys.stderr)
            return 1
        print(render_quality_ja(judged.quality))
        if judged.release_wanted:
            print(render_release_requests_ja([judged.release_wanted],
                                             releases_dir()))
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
        print(f"\n(ゲートで却下 — LLM不要。記録済み: {rec.id})")
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
    print(f"\n(記録済み: {rec.id} / status={status.value})")
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


def cmd_scout(args: argparse.Namespace) -> int:
    config = HawkeyeConfig.from_env()
    finnhub = FinnhubProvider()
    if not finnhub.available:
        print("scout には FINNHUB_API_KEY が必要です(無料キー: finnhub.io)",
              file=sys.stderr)
        return 1
    provider = CompositeProvider(YahooProvider(), finnhub)
    ledger = _ledger()
    # The window is derived from the previous run, so an irregular manual
    # cadence doesn't leave unscanned days (§5.2(1)). --days forces a fixed
    # lookback instead, for backfills and one-off exploration.
    window = (None if args.days
              else scan_window(date.today(), ledger.last_scan_at(), config))
    # Discovery stays with the calendar; the EPS numbers are re-read from
    # Yahoo before ranking (hawkeye/scout/verify.py). If yfinance is missing
    # the source reports itself unavailable and the scan runs on the
    # calendar's own figures rather than failing.
    numbers = YahooEarningsSource()
    if not numbers.available:
        print("yfinance が未インストールのため、決算数値はカレンダーの値のまま"
              "使います(pip install yfinance lxml)", file=sys.stderr)
    # The stock master turns each print into a stored quarter judged against
    # the consensus that was in force before it. Without a pre-registered row
    # the funnel reconstructs one and says so — it never silently treats an
    # after-the-fact estimate as what was knowable in advance (§6.1(D)).
    result = run_scout(finnhub, provider, config, days_back=args.days,
                       window=window, already_seen=ledger.seen_events(),
                       numbers_source=numbers if numbers.available else None,
                       stock_store=_stock_store(),
                       directory=EdgarDirectory(),
                       consensus_source=(YahooConsensusSource()
                                         if numbers.available else None),
                       facts=EdgarFacts(),
                       release_reader=DirectoryReleaseReader(releases_dir()))

    # Whatever isn't sent to the tribunal THIS run — from result.passed's
    # tail onward — is the ranking-cutoff tier (docs/MASTER_OVERVIEW.ja.md
    # §5.1, #4). Computed before record_scan() so it can be persisted in the
    # same breath as the scan itself, immediately below.
    sent_to_tribunal_n = max(args.evaluate or 0, args.open_cases or 0)

    scan_id = ledger.record_scan(
        params={"window_start": result.scan_start.isoformat(),
                "window_end": result.scan_end.isoformat(),
                "window_truncated": result.window_truncated,
                "days_back_override": args.days,
                "duplicates_skipped": result.duplicates,
                "min_eps_surprise": config.scout_min_eps_surprise_pct,
                **result.verification.as_dict()},
        scanned=result.scanned, screened=result.screened,
        enriched=result.enriched, gate_passed=len(result.passed),
        tickers=[c.ticker for c in result.passed])
    ledger.record_screened_candidates(
        scan_id, build_screened_candidates(result, scan_id, sent_to_tribunal_n))
    print(render_scout_ja(result))

    if result.release_wanted:
        print(render_release_requests_ja(result.release_wanted,
                                         releases_dir()))

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
            print(f"\n(記録済み: {rec.id} / status={status.value})")
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
    window = business_days_ahead(today, days)
    raw = finnhub.earnings_calendar(window[0], window[-1])
    targets = upcoming_prints(raw, today=today, business_days=days)
    if args.limit:
        targets = targets[:args.limit]
    print(f"対象期間: {window[0]} 〜 {window[-1]}(営業日{days}日)")
    print(f"決算予定で実績がまだ出ていない銘柄: {len(targets)} 件")
    if args.dry_run:
        for item in targets:
            print(f"- {item.report_date} {item.ticker} {item.fiscal_quarter} "
                  f"(EPS予想 {item.eps_estimate})")
        print("(--dry-run のため記録していません)")
        return 0

    source = YahooConsensusSource()
    if not source.available:
        print("yfinance が無いため、アナリスト人数と予想レンジは取得できません"
              "(Finnhubの点推定のみ事前登録します)", file=sys.stderr)
    report = capture_consensus(_stock_store(), targets,
                               source if source.available else None,
                               directory=EdgarDirectory())
    print(report_line(report))
    warn_if_nothing_captured(report)
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


def cmd_drops_report(args: argparse.Namespace) -> int:
    """Score the funnel's rejects (docs/MASTER_OVERVIEW.ja.md §5.2(3)).

    Both ledger tables are read: `screened_candidates` holds the candidates
    dropped before the tribunal, `recommendations` the ones that reached it.
    Reviewing only the former would leave the tribunal unscored; reviewing
    only the latter is the blind spot §5.1 was written about.
    """
    config = HawkeyeConfig.from_env()
    ledger = _ledger()
    provider = _provider()

    tracked = [from_screened(c) for c in ledger.screened_candidates()]
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

    sc = sub.add_parser("scout", help="scan earnings surprises for candidates")
    sc.add_argument("--days", type=int, default=None,
                    help="scan window in days (default: config)")
    sc.add_argument("--evaluate", type=int, default=0, metavar="N",
                    help="run the tribunal on the top N candidates (API mode)")
    sc.add_argument("--open-cases", type=int, default=0, metavar="N",
                    help="open session-mode cases for the top N candidates "
                         "(no API key; driven by /hawkeye-run)")
    sc.add_argument("--nav", type=float, default=100_000.0)
    sc.set_defaults(func=cmd_scout)

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

    sd = sub.add_parser("screened",
                        help="review candidates the scout funnel dropped "
                             "(docs/MASTER_OVERVIEW.ja.md §5.1)")
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
                             "(docs/MASTER_OVERVIEW.ja.md §5.2(3))")
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
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
