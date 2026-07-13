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
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from hawkeye.config import HawkeyeConfig, db_path
from hawkeye.contracts.models import (
    Catalyst,
    CatalystType,
    DecisionType,
    Outcome,
    Recommendation,
    RecommendationStatus,
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
from hawkeye.reports.render_ja import (
    render_recommendation_ja,
    render_scout_ja,
    render_signals_ja,
)
from hawkeye.scout.benchmark import cohort_of, cohort_stats, forward_return
from hawkeye.scout.scout import run_scout
from hawkeye.sentinel.monitor import check_position
from hawkeye.tribunal.pipeline import run_tribunal


def _provider() -> CompositeProvider:
    return CompositeProvider(YahooProvider(), FinnhubProvider())


def _ledger() -> Ledger:
    return Ledger(db_path())


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
    }
    brief = build_brief(args.ticker.upper(), catalyst, _provider(),
                        notes=args.notes or "", overrides=overrides)

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


def cmd_scout(args: argparse.Namespace) -> int:
    config = HawkeyeConfig.from_env()
    finnhub = FinnhubProvider()
    if not finnhub.available:
        print("scout には FINNHUB_API_KEY が必要です(無料キー: finnhub.io)",
              file=sys.stderr)
        return 1
    provider = CompositeProvider(YahooProvider(), finnhub)
    result = run_scout(finnhub, provider, config, days_back=args.days)

    ledger = _ledger()
    ledger.record_scan(
        params={"days_back": args.days or config.scout_days_back,
                "min_eps_surprise": config.scout_min_eps_surprise_pct},
        scanned=result.scanned, screened=result.screened,
        enriched=result.enriched, gate_passed=len(result.passed),
        tickers=[c.ticker for c in result.passed])
    print(render_scout_ja(result))

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


def cmd_benchmark(args: argparse.Namespace) -> int:
    ledger = _ledger()
    provider = _provider()
    today = date.today()
    samples: list[tuple[str, float]] = []
    skipped = 0
    for row in ledger.list():
        rec = ledger.get(row["id"])
        if rec is None:
            continue
        eval_day = rec.created_at.date()
        if (today - eval_day).days < args.horizon:
            skipped += 1
            continue
        try:
            bars = provider.daily_history(rec.ticker, days=400)
        except Exception:
            skipped += 1
            continue
        ret = forward_return(bars, eval_day, args.horizon)
        if ret is None:
            skipped += 1
            continue
        samples.append((cohort_of(rec), ret))

    print(f"# 📊 コホート・ベンチマーク(評価日から{args.horizon}日後のリターン)\n")
    if not samples:
        print(f"対象データなし(評価から{args.horizon}日経過した記録がまだありません)")
        return 0
    stats = cohort_stats(samples)
    label = {"BUY": "🟢 BUY(推奨)", "TRIBUNAL_PASS": "⚪ 審理で見送り",
             "GATE_REJECT": "🚧 ゲートで却下"}
    print("| コホート | 件数 | 平均 | 中央値 | 勝率 |")
    print("|---|---|---|---|---|")
    for cohort in ("BUY", "TRIBUNAL_PASS", "GATE_REJECT"):
        s = stats[cohort]
        if s["n"] == 0:
            print(f"| {label[cohort]} | 0 | - | - | - |")
        else:
            print(f"| {label[cohort]} | {s['n']} | {s['mean']:+.2f}% | "
                  f"{s['median']:+.2f}% | {s['win_rate']:.0%} |")
    buy, pas = stats["BUY"], stats["TRIBUNAL_PASS"]
    if buy["n"] > 0 and pas["n"] > 0:
        spread = buy["mean"] - pas["mean"]
        print(f"\nBUY − 見送り スプレッド: {spread:+.2f}%ポイント "
              f"({'✅ 絞り込みが価値を生んでいる方向' if spread > 0 else '⚠️ 絞り込みが価値を生んでいない — ロジック要見直し'})")
    if skipped:
        print(f"\n(スキップ: {skipped}件 — 期間未達またはデータ取得失敗)")
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
        print(f"{r['id']}  {r['ticker']:<6}  {r['status']:<12}  {r['created_at']}")
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
    ev.set_defaults(func=cmd_evaluate)

    sc = sub.add_parser("scout", help="scan earnings surprises for candidates")
    sc.add_argument("--days", type=int, default=None,
                    help="scan window in days (default: config)")
    sc.add_argument("--evaluate", type=int, default=0, metavar="N",
                    help="run the tribunal on the top N candidates (uses LLM)")
    sc.add_argument("--nav", type=float, default=100_000.0)
    sc.set_defaults(func=cmd_scout)

    bm = sub.add_parser("benchmark",
                        help="forward returns: BUY vs PASS vs gate-reject cohorts")
    bm.add_argument("--horizon", type=int, default=30,
                    help="days after evaluation (default 30)")
    bm.set_defaults(func=cmd_benchmark)

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
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
