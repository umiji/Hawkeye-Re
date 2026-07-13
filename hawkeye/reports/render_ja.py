"""Japanese-language report rendering (the only user-facing surface).

System internals are English (token economy); everything the user reads is
Japanese, per the project requirements.
"""
from __future__ import annotations

from hawkeye.contracts.models import (
    DecisionType,
    KillKind,
    Recommendation,
)
from hawkeye.sentinel.monitor import Signal

_CATALYST_JA = {
    "earnings_beat": "決算ビート(EPS/売上サプライズ)",
    "earnings_beat_raise": "決算ビート+ガイダンス引き上げ",
    "guidance_raise": "ガイダンス引き上げ",
    "earnings_overreaction": "決算への過剰反応",
    "product_launch": "新製品発表",
    "insider_buying": "インサイダー買い",
    "index_inclusion": "指数採用",
    "spinoff_restructuring": "スピンオフ・再編",
    "merger_acquisition": "M&A",
    "regulatory_approval": "規制当局の承認",
    "other": "その他",
}

_EDGE_JA = {
    "underreaction": "アンダーリアクション(市場の織り込み遅れ)",
    "overreaction": "オーバーリアクション(売られ過ぎ/買われ過ぎの修正)",
    "structural_flow": "構造的フロー(機械的な売買主体の存在)",
    "information_synthesis": "情報統合(公開情報の点と点が未接続)",
    "none_identified": "エッジ特定できず",
}

_KILL_JA = {
    KillKind.PRICE_BELOW: "株価が下回ったら",
    KillKind.PRICE_ABOVE: "株価が上回ったら",
    KillKind.TIME_STOP_DAYS: "保有日数が超えたら",
    KillKind.EVENT: "イベント発生時(要人判断)",
}


def _fmt(v, suffix="", nd=2):
    if v is None:
        return "不明"
    return f"{v:,.{nd}f}{suffix}"


def render_recommendation_ja(rec: Recommendation) -> str:
    is_buy = rec.verdict.decision == DecisionType.BUY
    s = rec.brief.snapshot
    lines: list[str] = []
    header = "🟢 投資提案(BUY)" if is_buy else "⚪ 見送り(PASS)"
    lines.append(f"# {header}: {rec.ticker} {rec.brief.company_name}")
    lines.append("")
    lines.append(f"- 提案ID: `{rec.id}`  作成: {rec.created_at:%Y-%m-%d %H:%M} UTC  "
                 f"モデル: {rec.model}")
    lines.append(f"- 現在値: ${_fmt(s.price)}  時価総額: {_fmt((s.market_cap or 0)/1e9 if s.market_cap else None, 'B USD')}  "
                 f"20日平均売買代金: {_fmt((s.avg_dollar_volume_20d or 0)/1e6 if s.avg_dollar_volume_20d else None, 'M USD')}")
    lines.append(f"- カタリスト: {_CATALYST_JA.get(rec.brief.catalyst.type.value, rec.brief.catalyst.type.value)}"
                 f"({rec.brief.catalyst.event_date}) — {rec.brief.catalyst.description}")
    if s.gap_on_event_pct is not None:
        lines.append(f"- イベント当日の値動き: {s.gap_on_event_pct:+.1f}%  "
                     f"イベント後の推移: {_fmt(s.change_since_event_pct, '%', 1)}  "
                     f"経過: {s.days_since_event}営業日")
    lines.append("")

    # Gates
    lines.append("## 入口ゲート判定")
    for g in rec.gate_report.results:
        mark = "✅" if g.passed and not g.unverified else ("⚠️" if g.unverified or not g.hard else "❌")
        note = f" — {g.note}" if g.note else ""
        val = f" (値={g.value:,.2f} / 基準={g.threshold:,.2f})" if g.value is not None and g.threshold is not None else ""
        lines.append(f"- {mark} {g.name}{val}{note}")
    lines.append("")

    # Thesis
    if rec.thesis is not None:
        t = rec.thesis
        lines.append("## ブルケース(推進役の論旨)")
        lines.append(t.summary)
        lines.append("")
        lines.append(f"- **エッジの種類**: {_EDGE_JA.get(t.edge_type.value, t.edge_type.value)}")
        lines.append(f"- **なぜ今ミスプライスなのか**: {t.edge_explanation}")
        lines.append(f"- **売り手は誰で、なぜ間違っているか**: {t.other_side}")
        lines.append(f"- **想定保有期間**: {t.expected_holding_days}日")
        lines.append("")
        lines.append("### 事前登録された主張(検証対象)")
        lines.append("| # | 主張 | 確率 | 期限 | 検証方法 |")
        lines.append("|---|------|------|------|----------|")
        for i, c in enumerate(t.claims, 1):
            lines.append(f"| {i} | {c.statement} | {c.probability:.0%} | "
                         f"{c.horizon_days}日 | {c.verification} |")
        lines.append("")
        lines.append("### シナリオ")
        lines.append("| シナリオ | 確率 | 目標株価 | 根拠 |")
        lines.append("|---------|------|---------|------|")
        for sc in t.scenarios:
            lines.append(f"| {sc.name} | {sc.probability:.0%} | "
                         f"${sc.price_target:,.2f} | {sc.rationale} |")
        lines.append("")
        lines.append("### キル基準(これに触れたら降りる)")
        for k in t.kill_criteria:
            detail = ""
            if k.level is not None:
                detail = f" ${k.level:,.2f}"
            elif k.days is not None:
                detail = f" {k.days}日"
            lines.append(f"- {_KILL_JA.get(k.kind, k.kind.value)}{detail}: {k.description}")
        lines.append("")

    # Attacks
    if rec.attack_report is not None:
        a = rec.attack_report
        lines.append("## 反証プロセス(攻撃役の指摘)")
        for atk in sorted(a.attacks, key=lambda x: -x.severity):
            kill = " 💀" if atk.is_kill_shot else ""
            lines.append(f"- **[深刻度{atk.severity}]** ({atk.category.value}){kill} "
                         f"{atk.statement}")
            if atk.evidence:
                lines.append(f"  - 根拠: {atk.evidence}")
        lines.append("")
        lines.append("### 最強のショートケース(あえて逆張りの視点)")
        lines.append(a.strongest_short_case)
        lines.append("")

    # Verdict
    lines.append("## 裁定")
    lines.append(f"- **判定**: {'買い推奨' if is_buy else '見送り'}  "
                 f"**確信度**: {rec.verdict.conviction:.0%}")
    if rec.verdict.expected_value_pct is not None:
        lines.append(f"- **シナリオ加重期待リターン**: {rec.verdict.expected_value_pct:+.1f}%")
    if rec.verdict.reward_risk is not None:
        lines.append(f"- **リワード/リスク比**: {rec.verdict.reward_risk:.2f}")
    lines.append("")
    lines.append(rec.verdict.rationale)
    if rec.verdict.addressed:
        lines.append("")
        lines.append("### 深刻な指摘への応答")
        for ad in rec.verdict.addressed:
            conv = "(キル基準に転換)" if ad.converted_to_kill_criterion else ""
            lines.append(f"- 指摘: {ad.attack_statement}")
            lines.append(f"  - 応答{conv}: {ad.response}")
    lines.append("")

    # Plan
    if rec.plan is not None and is_buy:
        p = rec.plan
        lines.append("## ポジション計画(リスク管理役)")
        lines.append(f"- 参考エントリー: ${p.entry_ref_price:,.2f}")
        lines.append(f"- 損切り: ${p.stop_price:,.2f}  目標: ${p.target_price:,.2f}")
        lines.append(f"- 株数: {p.shares:,}株 ≒ ${p.position_value:,.0f} "
                     f"(NAVの{p.position_pct_nav:.1f}%、リスク{p.risk_pct:.2f}%)")
        lines.append(f"- 最大保有日数: {p.max_holding_days}日(タイムストップ)")
        lines.append("")

    lines.append("---")
    lines.append("実行(Yes)/見送り(No)の最終判断と発注はユーザー自身が行ってください。"
                 "本レポートは投資助言ではなく、システムの検証記録です。")
    return "\n".join(lines)


def render_scout_ja(result) -> str:
    """ScoutResult -> Japanese shortlist report."""
    lines = [f"# 🔭 スカウト結果 ({result.scan_start} 〜 {result.scan_end})", ""]
    f = result.funnel()
    lines.append(f"ファネル: 決算イベント {f['scanned']}件 → サプライズ選別 "
                 f"{f['screened']}件 → 詳細取得 {f['enriched']}件 → "
                 f"ゲート通過 {f['gate_passed']}件")
    lines.append("")
    if result.passed:
        lines.append("## 候補ショートリスト(スコア順)")
        lines.append("| 順位 | ティッカー | イベント日 | EPSサプライズ | 売上サプライズ | 当日反応 | スコア |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, c in enumerate(result.passed, 1):
            gap = (f"{c.brief.snapshot.gap_on_event_pct:+.1f}%"
                   if c.brief and c.brief.snapshot.gap_on_event_pct is not None
                   else "不明")
            rev = (f"{c.revenue_surprise_pct:+.1f}%"
                   if c.revenue_surprise_pct is not None else "-")
            lines.append(f"| {i} | **{c.ticker}** | {c.event_date} | "
                         f"{c.eps_surprise_pct:+.1f}% | {rev} | {gap} | {c.score} |")
        lines.append("")
        lines.append("次の一手(検証にかける):")
        top = result.passed[0]
        lines.append("```")
        lines.append(f"hawkeye evaluate {top.ticker} --catalyst earnings_beat "
                     f"--event-date {top.event_date} \\")
        lines.append(f"  --description \"{top.brief.catalyst.description if top.brief else 'EPS surprise'}\"")
        lines.append("```")
        lines.append("(または `hawkeye scout --evaluate N` で上位N件を自動で審理にかけられます)")
    else:
        lines.append("ゲートを通過した候補はありませんでした。")
    if result.rejected:
        lines.append("")
        lines.append("## 却下された候補")
        for c in result.rejected:
            lines.append(f"- {c.ticker} ({c.event_date}, EPS {c.eps_surprise_pct:+.1f}%): "
                         f"{c.reject_reason}")
    return "\n".join(lines)


def render_signals_ja(ticker: str, signals: list[Signal]) -> str:
    if not signals:
        return f"✅ {ticker}: シグナルなし(事前登録済みの基準に抵触なし)"
    lines = [f"# ⚠️ {ticker}: シグナル検知 ({len(signals)}件)", ""]
    for sig in signals:
        badge = "🔴 売り推奨" if sig.severity == "sell" else "🟡 要レビュー"
        lines.append(f"- {badge} [{sig.kind}] {sig.message}")
    lines.append("")
    lines.append("売却の最終判断と発注はユーザー自身が行ってください。")
    return "\n".join(lines)
