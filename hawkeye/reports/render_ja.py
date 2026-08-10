"""Japanese-language report rendering (the only user-facing surface).

System internals are English (token economy); everything the user reads is
Japanese, per the project requirements.
"""
from __future__ import annotations

from datetime import datetime

from hawkeye.contracts.models import (
    DecisionType,
    KillKind,
    Recommendation,
    to_jst,
)
from hawkeye.reports.quality_ja import _flag_ja
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


def fmt_jst(value: datetime | str) -> str:
    """A stored timestamp as the user reads it: `2026-07-31 23:45 JST`.

    Takes either a datetime or the raw ISO string straight out of the
    ledger, since some listings print the stored column without parsing it.
    Records written before 2026-07-31 carry a `+00:00` offset and are
    converted here, so old and new rows read on the same clock.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value      # hand-edited/unknown format: show it verbatim
    return f"{to_jst(value):%Y-%m-%d %H:%M} JST"


def render_recommendation_ja(rec: Recommendation) -> str:
    is_buy = rec.verdict.decision == DecisionType.BUY
    s = rec.brief.snapshot
    lines: list[str] = []
    header = "🟢 投資提案(BUY)" if is_buy else "⚪ 見送り(PASS)"
    lines.append(f"# {header}: {rec.ticker} {rec.brief.company_name}")
    lines.append("")
    lines.append(f"- 提案ID: `{rec.id}`  作成: {fmt_jst(rec.created_at)}  "
                 f"モデル: {rec.model}")
    lines.append(f"- 現在値: ${_fmt(s.price)}  時価総額: {_fmt((s.market_cap or 0)/1e9 if s.market_cap else None, 'B USD')}  "
                 f"20日平均売買代金: {_fmt((s.avg_dollar_volume_20d or 0)/1e6 if s.avg_dollar_volume_20d else None, 'M USD')}")
    lines.append(f"- カタリスト: {_CATALYST_JA.get(rec.brief.catalyst.type.value, rec.brief.catalyst.type.value)}"
                 f"({rec.brief.catalyst.event_date}) — {rec.brief.catalyst.description}")
    if s.gap_on_event_pct is not None:
        lines.append(f"- イベント当日の値動き: {s.gap_on_event_pct:+.1f}%  "
                     f"イベント後の推移: {_fmt(s.change_since_event_pct, '%', 1)}  "
                     f"経過: {s.days_since_event}営業日")
    if s.eps_surprise_pct is not None or s.revenue_surprise_pct is not None:
        lines.append(f"- EPSサプライズ: {_fmt(s.eps_surprise_pct, '%', 1)}  "
                     f"売上サプライズ: {_fmt(s.revenue_surprise_pct, '%', 1)}"
                     f"(コンセンサス予想比、機械計算)")
    ia = rec.brief.insider_activity
    if ia is not None:
        lines.append(f"- インサイダー動向({ia.window_days}日): "
                     f"買い{ia.buyers}名 / 売り{ia.sellers}名  "
                     f"純株数{ia.net_shares:+,.0f}株")
    at = rec.brief.analyst_trend
    if at is not None:
        cur = f"強気{at.strong_buy}/買い{at.buy}/中立{at.hold}/売り{at.sell}/強気売り{at.strong_sell}"
        lines.append(f"- アナリスト格付け({at.period}): {cur}")
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


def render_backfill_ja(stats) -> str:
    """What the backfill of past quarters managed, and what it cannot do.

    The ceiling is stated every time rather than only when it bites. A reader
    who sees 「過去4四半期を取得」 and assumes the run of eight was checked
    would credit a name with a consistency nobody measured — and the numbers
    on the page look identical either way.
    """
    if not stats.tickers_attempted:
        return ""
    lines = [f"過去の決算の遡り取得: 上位 {stats.tickers_attempted}銘柄に"
             f"問い合わせ → 新たに記録 {stats.quarters_written}四半期 / "
             f"既に記録済みで手を付けなかった {stats.quarters_already_known}四半期"]
    if stats.tickers_unreachable:
        lines.append(f"  - {stats.tickers_unreachable}銘柄は問い合わせが"
                     "完了しませんでした(会社の事情ではなく通信の失敗です。"
                     "次回の走査で再試行します)")
    for reason, count in sorted(stats.skipped.items()):
        lines.append(f"  - {count}四半期: {_flag_ja(reason)}")
    lines.append("  ※ この経路で取れるのは**EPSだけ**で、売上は取得できません。"
                 "また**4四半期が上限**です(2026-08-10実測)。"
                 "「8四半期ぶりに1回だけ上振れ」という形は、"
                 "この記録では判別できません。")
    return "\n".join(lines)


def render_scout_ja(result) -> str:
    """ScoutResult -> Japanese shortlist report."""
    lines = [f"# 🔭 スカウト結果 ({result.scan_start} 〜 {result.scan_end})", ""]
    f = result.funnel()
    lines.append(f"ファネル: 決算イベント {f['scanned']}件 → サプライズ選別 "
                 f"{f['screened']}件 → 既出を除外 {f['duplicates']}件 → "
                 f"実績待ちで保留 {f.get('held', 0)}件 → "
                 f"詳細取得 {f['enriched']}件 → "
                 f"ゲート通過 {f['gate_passed']}件")
    # 「見送った」と「まだ判定していない」を混同させない。保留は会社についての
    # 判断ではなく、こちらのデータがまだ揃っていないという事実で、次回の走査で
    # 読み直す。待機期限を過ぎた分は打ち切って落選記録に残す。期限の時間数は
    # 設定値なので、ここに数字を書くと設定を変えた日から嘘になる。
    held = getattr(result, "held", [])
    if held:
        timed_out = [c for c in held if c.held_expired]
        line = (f"実績待ちの保留 {len(held)}件: 次回の走査で読み直します"
                f"(これは会社への判断ではなく、決算の数値がまだ届いていない"
                f"という事実です)")
        if timed_out:
            line += (f"。うち {len(timed_out)}件 は待機期限"
                     f"(`earnings_actual_wait_hours`)を過ぎたため"
                     f"打ち切りました: "
                     f"{'、'.join(c.ticker for c in timed_out[:8])}")
        lines.append(line)
    # どの銘柄が「決算専門サイト(EarningsWhispers)の数字で順位を付けられたか」。
    # 断られ方は3つに分けて出す — 意味も、次にやることも違うため。
    # 「まだ取り込まれていない」は待てば来る、「サイトに繋がらなかった」は
    # 会社についての事実ではない、「物差しが欠けている」は待っても増えない。
    n = getattr(result, "numbers", None)
    if n is not None and n.attempted:
        lines.append(
            f"決算数値の取得: {n.attempted}件を決算専門サイトに問い合わせ → "
            f"同サイトの数字で判定 {n.from_whispers}件 / "
            f"カレンダーの数字のまま {n.fell_back}件 / "
            f"前期のレコードが返った {n.stale}件 / "
            f"サイトに繋がらなかった {n.unreachable}件")
        if n.budget_exhausted:
            lines.append(f"⚠️ 問い合わせの上限({n.attempted}件)に達しました。"
                         "残りはカレンダーの数値のままです"
                         "(`scout_max_whispers` で調整)。")
    if getattr(result, "enrichment_ceiling_hit", False):
        lines.append("")
        lines.append("⚠️ **詳細取得の試行上限に達したため、ゲート通過候補が"
                     "揃う前に打ち切りました。** 候補が少ないのは決算が"
                     "静かだったからではなく、入口ゲートで落ちた銘柄が"
                     "多かったためです(`scout_max_enrich` で調整)。")
    if getattr(result, "window_truncated", False):
        lines.append("")
        lines.append("⚠️ **前回実行からの間隔が探索窓の上限を超えました。**"
                     f" {result.scan_start} より前の決算はスキャンしていません"
                     "(取りこぼしの可能性あり)。必要なら "
                     "`hawkeye scout --days N` で遡って実行してください。")
    # 発表済みの決算の数値が、あとから提供元に書き換えられることがある。
    # ADEA は 2026-08-05 発表の四半期で、EPSの実績値だけが翌日 $0.34 → $0.42
    # (+24%)に変わった。順位表より前に出すのは、先にショートリストを読んで
    # しまった読み手はもう訂正に反応しないため。台帳では古い行を残したまま
    # 新しい行を足しており、順位はすでに訂正後の数値で付けてある。
    revisions = getattr(result, "revisions", [])
    if revisions:
        lines.append("")
        lines.append(f"## ⚠️ 実績値が訂正されました ({len(revisions)}件)")
        lines.append("")
        lines.append("| 銘柄 | 四半期 | 項目 | 更新前 | 更新後 | 変化 |")
        lines.append("|---|---|---|---|---|---|")
        label = {"eps_actual": "EPS実績", "revenue_actual": "売上実績"}
        for r in revisions:
            pct = (f"{r.change_pct:+.1f}%" if r.change_pct is not None
                   else "-")
            before = "-" if r.before is None else f"{r.before:g}"
            after = "(取り下げ)" if r.after is None else f"{r.after:g}"
            lines.append(f"| {r.ticker} | {r.fiscal_quarter} "
                         f"| {label.get(r.field, r.field)} | {before} "
                         f"| {after} | {pct} |")
        lines.append("")
        lines.append("下の順位は訂正後の数値で付けています。訂正前の行も台帳に"
                     "残っているので、前回どの数値で順位を付けたかは後から"
                     "確認できます。")
    lines.append("")
    if result.passed:
        lines.append("## 候補ショートリスト(スコア順)")
        lines.append("| 順位 | ティッカー | イベント日 | EPSサプライズ | 売上サプライズ | 当日反応 | スコア |")
        lines.append("|---|---|---|---|---|---|---|")
        untrusted_seen = False
        for i, c in enumerate(result.passed, 1):
            gap = (f"{c.brief.snapshot.gap_on_event_pct:+.1f}%"
                   if c.brief and c.brief.snapshot.gap_on_event_pct is not None
                   else "不明")
            # A percentage the screen does not stand behind must not be shown
            # as if it did — it earns no score, and the reader has to be able
            # to see why a big number sits low in the ranking.
            eps_mark = "" if c.eps_surprise_trusted else " ⚠"
            rev_mark = "" if c.revenue_surprise_trusted else " ⚠"
            untrusted_seen = untrusted_seen or bool(
                eps_mark or (c.revenue_surprise_pct is not None and rev_mark)
                or c.conflicting_estimates)
            rev = (f"{c.revenue_surprise_pct:+.1f}%{rev_mark}"
                   if c.revenue_surprise_pct is not None else "-")
            ticker = c.ticker + ("†" if c.conflicting_estimates else "")
            lines.append(f"| {i} | **{ticker}** | {c.event_date} | "
                         f"{c.eps_surprise_pct:+.1f}%{eps_mark} | {rev} | "
                         f"{gap} | {c.score} |")
        lines.append("")
        if untrusted_seen:
            lines.append("⚠ = その数値は採点に使っていません(分母が小さすぎる、"
                         "または実績と予想の集計基準が食い違っている)。"
                         "†= 決算カレンダーが同じ決算に対して矛盾する予想値を"
                         "返したため、保守的な方を採用しています。")
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


_COHORT_JA = {
    "BUY": "採用(BUY)",
    "TRIBUNAL_PASS": "審理で見送り",
    "RANKING_CUTOFF": "ランキング下位",
    "GATE_REJECT": "入口ゲート落ち",
    "ENRICHMENT_CAP": "肉付け上限落ち",
}


def _num(value, suffix: str = "", digits: int = 2) -> str:
    return "—" if value is None else f"{value:+.{digits}f}{suffix}"


def render_drop_review_ja(
    checkpoint: str,
    horizon_days: int,
    index_ticker: str,
    results: list,
    pending: int,
    censored: dict,
    cohort_table: dict,
    gate_table: dict,
    flagged: list,
    min_samples: int,
    suppressed: int = 0,
    suppressed_reason: str = "",
) -> str:
    """落選候補レビューの結果(docs/design/MASTER_OVERVIEW.ja.md §5.2(3))."""
    lines = [
        f"# 🔍 落選候補レビュー — T+{horizon_days}営業日時点 ({checkpoint})",
        "",
        f"判定済み {len(results)}件 / 観測期間が未経過 {pending}件",
    ]
    censored_n = sum(censored.values())
    if censored_n:
        lines.append(
            f"⚠️ **追跡不能 {censored_n}件** — 株価履歴を取得できませんでした"
            "(上場廃止・銘柄コード変更・買収・API障害)。"
            "取得できない銘柄は最悪の結果を出したものである場合が多く、"
            "**これを無視すると各群の平均が実態より良く見えます**(生存者バイアス)。")
        for cohort, n in censored.items():
            if n:
                lines.append(f"  - {_COHORT_JA.get(cohort, cohort)}: {n}件")
    lines.append("")
    lines.append(
        f"α = 実リターン − β × {index_ticker}の同期間リターン(βは過去250営業日)、"
        "z = α ÷ (ATR% × √営業日数)。**上振れ・下振れの両方**を数えます。")
    lines.append("")

    lines.append("## 段階別")
    lines.append("")
    lines.append("| 段階 | 件数 | 平均α | 中央α | 上振れ z≥1.5 | 下振れ z≤-1.5 | α算出不可 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for cohort, row in cohort_table.items():
        name = _COHORT_JA.get(cohort, cohort)
        if row["n"] < min_samples:
            name += " ※"
        lines.append(
            f"| {name} | {row['n']} | {_num(row['mean_alpha'], '%')} | "
            f"{_num(row['median_alpha'], '%')} | {row['up_outliers']} | "
            f"{row['down_outliers']} | {row['n_alpha_missing']} |")
    lines.append("")
    lines.append(
        f"※ = サンプルが{min_samples}件未満。**この段階の数字を根拠に閾値や"
        "スコア式を変更してはいけません**(§5.2(3) 過剰最適化の歯止め)。")
    lines.append("")

    # 見出しの正確さは重要。ここに出るのは「入口ゲートで落ちた候補」だけでは
    # なく、審理まで通った候補が個別に不合格・未検証だったゲート項目も含む。
    lines.append("## ゲート項目別(不合格・未検証だった項目。段階は問わない)")
    lines.append("")
    if not gate_table:
        lines.append("(該当なし)")
    else:
        lines.append("| ゲート | 件数 | 平均α | 上振れ | 下振れ |")
        lines.append("|---|---:|---:|---:|---:|")
        for gate, row in gate_table.items():
            label = gate if row["n"] >= min_samples else f"{gate} ※"
            lines.append(
                f"| {label} | {row['n']} | {_num(row['mean_alpha'], '%')} | "
                f"{row['up_outliers']} | {row['down_outliers']} |")
        lines.append("")
        lines.append(
            "平均が高いゲートは「そのゲートが取りこぼしを生んでいる」候補です。"
            "ただし**下振れの件数を必ず併せて見てください** — 下振れのほうが多い"
            "ゲートは、正しく仕事をしています。")
    lines.append("")

    lines.append(f"## 要調査({len(flagged)}件、|z| ≥ 1.5)")
    lines.append("")
    if suppressed:
        # 除外した件数は必ず出す。黙って隠すと「調べたが何も無かった」と
        # 「そもそも見ていない」の区別がつかなくなる。
        lines.append(
            f"（この一覧からは **{suppressed}件を除外**しています — "
            f"{suppressed_reason}）")
        lines.append("")
    if not flagged:
        lines.append("(該当なし)")
    else:
        for r in flagged:
            badge = "🔺" if r.direction == "up" else "🔻"
            stage = _COHORT_JA.get(r.cohort, r.cohort)
            gates = f" / {', '.join(r.failed_gates)}" if r.failed_gates else ""
            lines.append(
                f"- {badge} **{r.ticker}** α={_num(r.alpha_pct, '%')} "
                f"z={_num(r.z, '', 1)} — {stage}{gates} / 判断日 {r.decision_date}")
            if r.reject_reason:
                lines.append(f"  - 落選理由: {r.reject_reason}")
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


_MISS_CATEGORY_JA = {
    "gate_threshold_too_strict": "ゲートの基準が厳しすぎた",
    "score_formula_wrong": "スコアの付け方が実力を捉えていなかった",
    "enrichment_cap": "肉付け上限で見る前に落とした",
    "data_gap": "データ欠損(基準ではなく取得の問題)",
    "collection_gap": "収集の欠陥(判断日前に公開されていたのに持っていなかった)",
    "unforeseeable": "予見不能(判断日より後に起きたこと)",
    "gate_correct": "ゲートは正しかった(下振れ)",
    "other": "その他(要記述)",
}


def render_drop_cycle_ja(
    checkpoint: str,
    measured: list,
    investigated: list,
    cohort_counts: dict,
    censored: dict,
    pending: int,
    skipped: int,
    remaining: dict,
    ready: list,
    min_samples: int,
    previous_total: int = 0,
) -> str:
    """落選候補レビュー1巡分の報告(§5.2(3))。

    **20件に達していなくても毎回出します。** 「今回は該当なし」も結果であり、
    黙って何も出さないのと、見たうえで何も無かったのとは、読む側からは
    区別がつかないためです。
    """
    n = len(measured) + len(investigated)
    ups = [r for r in measured + investigated if r.z >= 1.5]
    downs = [r for r in measured + investigated if r.z <= -1.5]

    lines = [
        f"# 🔍 落選候補レビュー — {checkpoint} の巡回結果",
        "",
        "## 1. 今回測ったもの",
        "",
        f"- 今回測定: **{n}件**(うち個別調査を行ったもの {len(investigated)}件)",
        f"- 累計: **{previous_total + n}件**(前回まで {previous_total}件 / 今回 +{n}件)",
    ]
    if pending:
        lines.append(f"- 観測期間が未経過のため今回は対象外: {pending}件"
                     "(異常ではありません。日数が経てば自動的に対象になります)")
    if skipped:
        lines.append(f"- 記録済みのため再測定しなかったもの: {skipped}件"
                     "(同じ銘柄を測り直せないようにしてあります)")
    lines.append("")

    if cohort_counts:
        lines.append("| 落選した段階 | 件数 |")
        lines.append("|---|---:|")
        for cohort, count in cohort_counts.items():
            lines.append(f"| {_COHORT_JA.get(cohort, cohort)} | {count} |")
        lines.append("")

    lines.append("## 2. 大きく動いたもの(外れ値)")
    lines.append("")
    lines.append(
        "「その銘柄自身の平常の値幅に対して何倍動いたか」(z)が 1.5 以上のものを"
        "外れ値とします。**上振れだけでなく下振れも数えます** — "
        "下振れは「落として正解だった」証拠なので、これを見ないと"
        "「基準を緩めよう」という方向にしか結論が出ません。")
    lines.append("")
    if not ups and not downs:
        lines.append("**該当なし。** 今回測った範囲では、判断が大きく外れた銘柄は"
                     "ありませんでした(これは正常な結果です)。")
    else:
        lines.append(f"- 上振れ(見送ったのに大きく上がった): **{len(ups)}件**"
                     + (f" — {'、'.join(r.ticker for r in ups)}" if ups else ""))
        lines.append(f"- 下振れ(落として正解だった): **{len(downs)}件**"
                     + (f" — {'、'.join(r.ticker for r in downs)}" if downs else ""))
    lines.append("")

    lines.append("## 3. 個別に調べたもの")
    lines.append("")
    if not investigated:
        lines.append("該当なし(外れ値が無かったか、すべて個別調査の対象外の段階でした)。")
        lines.append("")
    for r in investigated:
        label = (_MISS_CATEGORY_JA.get(r.miss_category.value, r.miss_category.value)
                 if r.miss_category else "未分類")
        lines.append(f"### {r.ticker}(判断日 {r.decision_date} / "
                     f"α {r.alpha_pct:+.2f}% / z {r.z:+.2f})")
        lines.append("")
        lines.append(f"- **何が起きたか**: {r.what_happened or '—'}")
        if r.visible_evidence:
            lines.append("- **判断時点の記録から引用できる根拠**:")
            for e in r.visible_evidence:
                lines.append(f"  - 「{e}」")
        else:
            lines.append("- **判断時点の記録から引用できる根拠**: なし"
                         "(引用の無い説明は物語である可能性が高い点に注意)")
        lines.append(f"- **分類**: {label}")
        if r.proposed_change:
            lines.append(f"- **改訂の提案**: {r.proposed_change.target} を "
                         f"{r.proposed_change.direction} "
                         f"({r.proposed_change.rationale})")
        if r.notes:
            lines.append(f"- 備考: {r.notes}")
        lines.append("")

    lines.append(f"## 4. 分類ごとの累計と、改訂案を起草するまでの残り")
    lines.append("")
    lines.append(f"同じ原因が **{min_samples}件** たまった分類だけが改訂案の対象です。"
                 "1〜2件で基準をいじると、たまたま当たった1件に合わせて仕組みを"
                 "歪めることになるためです。")
    lines.append("")
    lines.append("| 分類 | あと何件で起草対象になるか |")
    lines.append("|---|---:|")
    for key, left in sorted(remaining.items(), key=lambda kv: kv[1]):
        label = _MISS_CATEGORY_JA.get(key, key)
        cell = "**到達**" if left == 0 else f"あと {left} 件"
        lines.append(f"| {label} (`{key}`) | {cell} |")
    lines.append("")

    if ready:
        names = "、".join(f"`{k}`" for k in ready)
        lines.append(f"➡️ **{names} が {min_samples}件に到達しました。** "
                     "改訂案を起草し `strategy/revisions/` に保存します"
                     "(採否を決めるのはユーザーです)。")
    else:
        lines.append(f"➡️ 到達した分類はありません。**改訂案は起草しません。**")
    lines.append("")

    lines.append("## 5. 測定できなかったもの")
    lines.append("")
    censored_n = sum(censored.values())
    if not censored_n:
        lines.append("該当なし。")
    else:
        lines.append(
            f"⚠️ **{censored_n}件** は株価履歴を取得できず測定できませんでした"
            "(上場廃止・銘柄コード変更・買収・API障害)。"
            "取得できない銘柄は最悪の結果を出したものである割合が高いため、"
            "**これを黙って除くと各群の平均が実態より良く見えます**"
            "(生存者バイアス)。件数を必ず添えて読んでください。")
        for cohort, count in censored.items():
            if count:
                lines.append(f"  - {_COHORT_JA.get(cohort, cohort)}: {count}件")
    return "\n".join(lines)
