"""Japanese rendering of the three-leg earnings judgment.

The reader is a decision-maker, not a code reviewer, so a flag never appears
as a bare identifier: `thin_coverage` reads as 「アナリスト人数が下限未満」
followed by what that does to the judgment. The rule this whole design turns
on — 未検証 is not a soft pass — is stated in the output rather than left for
the reader to infer from a missing score.
"""
from __future__ import annotations

from typing import Optional

from hawkeye.ledger.stocks import StockHistory
from hawkeye.scout.quality import (
    EarningsQuality,
    LegStatus,
    LegVerdict,
    QuarterVerdict,
)

_LEG = {"eps": "EPS", "revenue": "売上", "guidance": "ガイダンス"}
_STATUS = {LegStatus.BEAT: "上振れ", LegStatus.MISS: "下振れ",
           LegStatus.INLINE: "ほぼ一致", LegStatus.UNVERIFIED: "未検証",
           LegStatus.ABSENT: "開示なし"}
_VERDICT = {
    QuarterVerdict.GOOD_QUARTER: "良い決算(EPS・売上がともに上振れ、"
                                 "ガイダンスも下振れていない)",
    QuarterVerdict.MIXED: "強弱まちまち",
    QuarterVerdict.WEAK: "弱い決算(いずれかの柱が下振れ)",
    QuarterVerdict.UNVERIFIED: "判定不能(EPSが検証できていない)",
}
_FLAG = {
    "actual_disputed": "実績値が2ソースで食い違い(小さい方の実績×大きい方の"
                       "予想で評価しているため、どちらの数字でも成立する読み"
                       "だが、GAAP/調整後のどちらの基準かは未決着。この系では"
                       "決着させる手段がありません)",
    "finnhub_actual_conflict": "Finnhubが同じ決算に矛盾する実績値を返しており、"
                               "同社の実績は使用不能",
    "single_source_actual": "実績値のソースが1つだけ",
    "consensus_disputed": "コンセンサスが2ソースで食い違い(保守的な方で評価)",
    "single_source_consensus": "コンセンサスのソースが1つだけ"
                               "(決算前の事前登録が無い)",
    "thin_coverage": "アナリスト人数が下限未満",
    "estimate_too_small": "予想の絶対値が小さすぎて率が意味を持たない",
    "sources_disagree_on_direction": "上振れ/下振れの向きがソースで割れている",
    "no_actual": "実績値を取得できていない",
    "guidance_not_published": "会社がガイダンスを開示していない",
    "no_forward_consensus_to_compare": "比較対象の翌四半期コンセンサスが無い",
    "guidance_period_not_comparable": "会社が示した見通しの期間が翌四半期では"
                                      "ないため比較していない(通期見通しを"
                                      "翌四半期コンセンサスと比べると、期間の"
                                      "長さの差がそのまま偽の上振れになる)",
    "on_eps": "EPSレンジの中央値で比較",
    "on_revenue": "売上レンジの中央値で比較(EPSレンジの開示が無いため)",
}


def _flag_ja(flag: str) -> str:
    # `guided_FY2026` carries the period itself, so it cannot sit in the table
    # above; without this branch the reader would see the raw flag name.
    if flag.startswith("guided_"):
        return f"会社が示した見通しの期間: {flag[len('guided_'):]}"
    body = flag[len("revenue_"):] if flag.startswith("revenue_") else flag
    text = _FLAG.get(body, body)
    return f"売上: {text}" if flag.startswith("revenue_") else text


def render_leg_ja(leg: LegVerdict) -> str:
    head = f"  {_LEG.get(leg.leg, leg.leg)}: {_STATUS[leg.status]}"
    if leg.surprise_pct is not None:
        head += f" {leg.surprise_pct:+.1f}%"
        pair = []
        if leg.yahoo_surprise_pct is not None:
            pair.append(f"Yahoo {leg.yahoo_surprise_pct:+.1f}%")
        if leg.finnhub_surprise_pct is not None:
            pair.append(f"Finnhub {leg.finnhub_surprise_pct:+.1f}%")
        if len(pair) == 2:
            head += f"(保守的な読み / {' ・ '.join(pair)})"
    if leg.analysts is not None:
        head += f" [アナリスト{leg.analysts}人]"
    lines = [head]
    # 実績値そのものが割れている場合は、両方の数字を出す。2026-08-03(b) 以降、
    # 食い違ったままでも(保守的な読みで)上振れ判定になり得るので、読み手が
    # 「何と何が食い違っているのか」を目で確認できないと検算のしようがない。
    if "actual_disputed" in leg.flags:
        pair = [f"Yahoo {v:g}" for v in (leg.actual_yahoo,) if v is not None]
        pair += [f"Finnhub {v:g}" for v in (leg.actual_finnhub,)
                 if v is not None]
        if pair:
            lines.append(f"    実績値: {' / '.join(pair)}"
                         f"(評価に使ったのは {leg.actual:g})")
    lines += [f"    - {_flag_ja(f)}" for f in leg.flags]
    return "\n".join(lines)


def render_quality_ja(quality: EarningsQuality) -> str:
    lines = [f"{quality.ticker} {quality.fiscal_quarter} の決算判定: "
             f"{_VERDICT[quality.verdict]}",
             f"スコア {quality.score}"]
    lines += [render_leg_ja(leg) for leg in quality.legs]
    # Quarter-level warnings, which belong to the print rather than to one
    # leg. `unadjusted` is the reason this whole design exists — AMZN's
    # headline beat was a one-off — so it must never be stored and then left
    # off the page.
    quarter_only = [f for f in quality.flags
                    if f not in {g for leg in quality.legs for g in leg.flags}]
    if quarter_only:
        lines.append("  この決算そのものへの注意:")
        lines += [f"    - {_flag_ja(f)}" for f in quarter_only]
    if quality.verdict is QuarterVerdict.UNVERIFIED:
        lines.append("  ※ 未検証は「問題なし」ではありません。順位付けの点数は"
                     "ゼロとして扱い、既知の不明点として審理に渡します。")
    return "\n".join(lines)


def render_stock_history_ja(history: StockHistory) -> str:
    """One company: what we know, when we looked, and what we decided."""
    stock = history.stock
    lines = [f"# {stock.ticker} {stock.name}".rstrip(),
             f"ID: {stock.id}"
             + (f" / 取引所: {stock.exchange}" if stock.exchange else "")
             + (f" / セクター: {stock.sector}" if stock.sector else "")]
    if stock.last_reviewed_fiscal_quarter:
        stage = (stock.last_stage_reached.value
                 if stock.last_stage_reached else "-")
        lines.append(f"直近の審査: {stock.last_reviewed_fiscal_quarter} "
                     f"({stage})")
    else:
        lines.append("直近の審査: まだ審査していません")
    # 調査対象から外れている銘柄は、コンセンサスの事前登録が止まります。
    # 止まっている事実と、その理由・判定日が読めないと、あとから「なぜこの
    # 銘柄だけ予想の履歴が無いのか」を追えなくなります。
    if stock.investigation_target is False:
        when = (stock.investigation_checked_at.date()
                if stock.investigation_checked_at else "不明")
        lines.append(f"調査対象: 対象外({stock.investigation_reason} / "
                     f"判定日 {when})。コンセンサスの事前登録を行いません"
                     f"(判定は一定期間で失効し、再度対象に戻ります)")

    lines.append("\n## 決算実績(記録のある四半期のみ)")
    if not history.prints:
        lines.append("(記録なし。この銘柄はまだ決算を1件も見ていません)")
    for row in history.prints:
        consensus = history.consensus_for(row.fiscal_quarter)
        lines.append(
            f"- {row.fiscal_quarter} ({row.report_date}) 出所={row.source.value}"
            f" / EPS実績={_num(row.eps_actual)}"
            f" / カレンダーのEPS実績={_list(row.eps_actual_rows)}"
            f" / 確定コンセンサス="
            + (f"{_num(consensus.eps_avg)}({consensus.kind.value})"
               if consensus else "なし"))
        if row.contamination_flags:
            lines.append("    - 注意: "
                         + " / ".join(_flag_ja(f)
                                      for f in row.contamination_flags))

    lines.append("\n## 過去の判断")
    if not history.decisions:
        lines.append("(審理まで進んだ記録はありません)")
    for decision in history.decisions:
        lines.append(f"- {decision['created_at'][:10]} {decision['id']} "
                     f"{decision.get('decision') or '-'} "
                     f"/ status={decision['status']}")
    if history.screened:
        lines.append(f"\n## 落選記録: {len(history.screened)} 件")
        for row in history.screened[-5:]:
            lines.append(f"- {str(row['recorded_at'])[:10]} {row['stage']} "
                         f"(EPS {row.get('eps_surprise_pct')}%)")
    return "\n".join(lines)


def _num(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:g}"


def _list(values: list[float]) -> str:
    return "-" if not values else "/".join(f"{v:g}" for v in values)
