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
    "vendors_report_different_actuals":
        "同じ決算の実績値を、決算カレンダーは別の数字で報告している"
        "(判定には使っていません — 上の率は片方の提供元の実績と、同じ提供元の"
        "予想だけで計算しています。差はたいていGAAPと調整後の基準差ですが、"
        "この系では決着させる手段がありません)",
    "finnhub_actual_conflict": "Finnhubが同じ決算に矛盾する実績値を返しており、"
                               "同社の実績は使用不能",
    "thin_coverage": "アナリスト人数が下限未満",
    "estimate_too_small": "予想の絶対値が小さすぎて率が意味を持たない",
    "no_consensus": "比較対象のコンセンサスを取得できていない",
    "no_actual": "実績値を取得できていない",
    "guidance_not_published": "会社がガイダンスを開示していない",
    # こちらのデータの穴であって、会社についての事実ではない。この2つが無かった
    # 頃は、決算専門サイトが答えなかった銘柄が「会社が開示していない」として
    # 落選記録に永久に残っていた（2026-08-11 修正）。
    "no_summary_from_feed": "決算専門サイトがこの決算の文章を返さなかったため、"
                            "見通しを読む対象がありません"
                            "(会社が開示していないという意味ではありません)",
    "feed_not_asked": "この銘柄は決算専門サイトに問い合わせていないため、"
                      "見通しを読む対象がありません"
                      "(順位が下位で問い合わせ上限に届かなかった)",
    "no_forward_consensus_to_compare": "比較対象の翌四半期コンセンサスが無い",
    "guidance_period_not_comparable": "会社が示した見通しの期間が翌四半期でも"
                                      "通期でもないため比較していない(期間の"
                                      "長さが違う数字を比べると、その差が"
                                      "そのまま偽の上振れになる)",
    "no_full_year_consensus_to_compare": "会社は通期の見通しを示しているが、"
                                         "比較対象の通期コンセンサスを"
                                         "取得できていないため比較していない",
    "full_year_consensus_is_another_year": "取得できている通期コンセンサスが、"
                                           "会社の見通しとは別の年度のもの"
                                           "なので比較していない",
    # ガイダンス（会社が自分で出す業績見通し）は文章の中にしか無いので、
    # 2026-08-10からはエージェント（AI）に読ませています。読めなかったときの
    # 理由は3種類に分かれ、意味がまったく違います。①は会社の事情、②はこちらの
    # 不具合、③は通信の失敗です。「開示なし」の一語にまとめると②と③が消えます。
    "pending_extraction": "まだ読み取っていない(決算専門サイトの文章は取得済み。"
                          "AIによる読み取りが未実行です。"
                          "hawkeye guidance queue で処理してください)",
    "no_guidance_in_source": "会社が今後の見通しを述べていない(①決算専門サイトの"
                             "文章に見通しの記述そのものが無い。よくあることで、"
                             "減点はしません)",
    "no_number_in_source": "会社は見通しに触れているが、比較できる数字が"
                           "書かれていない(①)",
    "open_ended_range": "会社の見通しに上限が無い(「$6.00超」など)。中央値を"
                        "作れないので比較していない(①会社が意図的に上を"
                        "開けている数字に、こちらで上限を捏造しないため)",
    "quote_not_in_source": "②読み取りに失敗: AIが引用した英文が、元の文章の中に"
                           "見つからなかったため、読み取り結果ごと破棄した"
                           "(でっち上げの検出。この件数が増えるなら指示文か"
                           "検証条件の方が間違っています)",
    "quoted_the_wrong_sentence": "②読み取りに失敗: AIが引用したのは会社の見通し"
                                 "ではなく、前回の見通しかアナリスト予想の文だった"
                                 "ため破棄した(3つが同じ形で並んでいるため"
                                 "起きうる)",
    "period_unreadable": "②読み取りに失敗: AIが返した対象期間の書き方が想定外"
                         "だった",
    "period_not_next_quarter": "②読み取りに失敗: AIが返した対象期間が翌四半期でも"
                               "通期でもなく、比較する物差しが存在しない",
    "extraction_call_failed": "③AIの呼び出しが完了しなかった(会社の事情でも"
                              "読み取りの失敗でもありません。再実行で解消する"
                              "可能性があります)",
    "guidance_scope_qualified": "会社が示した見通しに条件が付いており、"
                                "比較対象のコンセンサスは同じ条件で作られて"
                                "いないため比較していない(例: 会社は特定の"
                                "事業を除いた数字を出し、アナリスト予想は"
                                "その事業を含んでいる。除いた分を当てずっぽうで"
                                "足し戻すことはしません)",
    # 決算専門サイトに問い合わせたが、その数字を使わなかった理由
    # （`hawkeye/scout/numbers.py`）。走査レポートには件数として出ていましたが、
    # 銘柄ごとの理由には日本語訳がなく、点検表で生の識別子が読み手に届いていました。
    # 3種類に分かれ、次にやることが違います —
    # ①待てば解消する ②待っても解消しない ③通信の失敗。
    "whispers_no_record": "①この銘柄のレコードが決算専門サイトにまだありません"
                          "(取り込み待ち。次回の走査で読み直します)",
    "whispers_previous_quarter": "①決算専門サイトが返したのは前四半期の"
                                 "レコードでした(同サイトは1社1決算しか持たず、"
                                 "取り込みが1日ほど遅れます。次回読み直します)",
    "whispers_later_print": "①決算専門サイトが返したのは、今回より後の決算の"
                            "レコードでした",
    "whispers_announcement_time_missing": "①決算専門サイトのレコードに発表時刻が"
                                          "無く、今回の決算のものか確認できません"
                                          "でした",
    "whispers_eps_incomplete": "②決算専門サイトのレコードにEPSの実績か予想の"
                               "どちらかが欠けており、片方だけでは率を計算"
                               "できません(待っても増えません)",
    "whispers_revenue_incomplete": "②決算専門サイトのレコードに売上の実績か"
                                   "予想のどちらかが欠けています(待っても"
                                   "増えません)",
    "whispers_unreachable": "③決算専門サイトに接続できませんでした"
                            "(会社についての事実ではありません。再実行で"
                            "解消する可能性があります)",
    "whispers_server_error": "③決算専門サイトがエラーを返しました。同じ銘柄で"
                             "毎回再現するなら、待っても同じ結果です",
    # 遡り取得（タスク10）で入れた行の但し書き。この経路は決算カレンダー側の
    # 銘柄別履歴で、EPSしか返らず、4四半期しか返りません（2026-08-10実測）。
    # 読み手が「なぜこの行には売上が無いのか」「なぜ日付が発表日でないのか」を
    # 追えないと、欠けているのが会社の事情なのか取得経路の限界なのか分かりません。
    "report_date_is_period_end": "この行は遡って取得したものです。日付は決算の"
                                 "発表日ではなく、対象四半期の末日です"
                                 "(取得経路が発表日を返さないため)",
    "repeated_actual": "取得元が、別々の四半期に同じEPS実績を返しました。"
                       "どちらかが前の四半期の使い回しですが、どちらが古いのかを"
                       "示す情報が応答に無いため、両方に印を付けています"
                       "(実測: AAPLの4行中2行)",
    "no_fiscal_quarter": "取得元が年度・四半期を返さなかったため、どの四半期の"
                         "実績か決められず記録していません",
    "no_period_date": "取得元が対象期間の末日を返さなかったため記録していません",
    "no_actual": "取得元が実績値を返さなかったため記録していません",
    "on_eps": "EPSレンジの中央値で比較",
    "on_revenue": "売上レンジの中央値で比較(EPSレンジの開示が無いため)",
}


def _flag_ja(flag: str) -> str:
    # `guided_FY2026` carries the period itself, so it cannot sit in the table
    # above; without this branch the reader would see the raw flag name.
    if flag.startswith("guided_"):
        return f"会社が示した見通しの期間: {flag[len('guided_'):]}"
    # `against_FY2026` says which period's consensus the percentage above was
    # measured against. A +13% beat means a different thing for a year than
    # for a quarter, so the period cannot be left off the line.
    if flag.startswith("against_"):
        return f"比較した期間: {flag[len('against_'):]}のコンセンサス"
    body = flag[len("revenue_"):] if flag.startswith("revenue_") else flag
    text = _FLAG.get(body, body)
    return f"売上: {text}" if flag.startswith("revenue_") else text


_SOURCE_JA = {"whispers": "決算専門サイト", "finnhub": "決算カレンダー",
              "calendar": "決算カレンダー"}


def render_leg_ja(leg: LegVerdict) -> str:
    head = f"  {_LEG.get(leg.leg, leg.leg)}: {_STATUS[leg.status]}"
    if leg.surprise_pct is not None:
        head += f" {leg.surprise_pct:+.1f}%"
        # どの提供元の数字で計算したのかを必ず添える。率の分子(実績)と分母
        # (予想)は必ず同じ提供元から取っており、それがこの数字の意味を決める。
        if leg.source:
            head += f"({_SOURCE_JA.get(leg.source, leg.source)}の実績と予想)"
    if leg.analysts is not None:
        head += f" [アナリスト{leg.analysts}人]"
    lines = [head]
    # もう一方の提供元が別の実績値を報告している場合は、両方の数字を出す。
    # 判定には使っていないが、読み手が「何と何が食い違っているのか」を目で
    # 確認できないと検算のしようがない。
    if "vendors_report_different_actuals" in leg.flags:
        lines.append(f"    実績値: 判定に使用 {leg.actual:g} / "
                     f"決算カレンダー {leg.other_actual:g}")
    lines += [f"    - {_flag_ja(f)}" for f in leg.flags]
    # 比較を見送ったときは、その根拠になった会社自身の文言をそのまま出す。
    # 「条件が付いていたので比較しませんでした」だけでは、その判断が妥当
    # だったのか読み手に検算しようがない(原文は英語のまま。要約サイトの
    # 文章そのものであって、こちらの言い換えではないことが分かるように)。
    if leg.excerpt:
        lines.append(f'      原文: "{leg.excerpt}"')
    return "\n".join(lines)


def render_quality_ja(quality: EarningsQuality) -> str:
    lines = [f"{quality.ticker} {quality.fiscal_quarter} の決算判定: "
             f"{_VERDICT[quality.verdict]}",
             f"スコア {quality.score}"]
    lines += [render_leg_ja(leg) for leg in quality.legs]
    # 「囁き予想」は、証券会社のアナリストが正式に出す予想平均(コンセンサス)
    # とは別に、決算専門サイトが集計している非公式の市場予想。コンセンサスより
    # 高いのが通例(実測11銘柄中11銘柄)なので、これを超えたかどうかは
    # コンセンサス超えより厳しい判定になる。スコアを動かしている以上、
    # 読み手に数字そのものを見せないと検算できない。
    if quality.whisper_beat_pct is not None:
        cleared = quality.whisper_beat_pct > 0
        phrase = (f"を上回った" if cleared else "に届かなかった")
        lines.append(f"  囁き予想(非公式の市場予想) {quality.whisper:g} "
                     f"{phrase}: {quality.whisper_beat_pct:+.1f}%")
        if not cleared:
            lines.append("    ※ 届かなくても減点はしません(加点のみ)。")
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
            + (f"{_consensus_eps_ja(consensus)}({consensus.kind.value})"
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


def _consensus_eps_ja(consensus) -> str:
    """The EPS expectation on record, whichever kind of figure it is.

    A distribution (`eps_avg`) and the earnings calendar's single point
    estimate (`eps_calendar`) are deliberately stored apart, and a backfilled
    quarter only ever has the point. Printing `eps_avg` alone rendered those
    rows as 「コンセンサス -」 — an expectation we DID retrieve, shown to the
    reader as one we never had, which is exactly the check they are reading
    this page to perform.
    """
    if consensus.eps_avg is not None:
        return _num(consensus.eps_avg)
    if consensus.eps_calendar is not None:
        return f"{_num(consensus.eps_calendar)}(カレンダーの単一予想)"
    return "-"


def _num(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:g}"


def _list(values: list[float]) -> str:
    return "-" if not values else "/".join(f"{v:g}" for v in values)
