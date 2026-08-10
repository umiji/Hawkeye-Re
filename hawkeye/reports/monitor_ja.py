"""Japanese rendering of the inspection table (§10).

Generated here rather than asked of an agent in a prompt: a step the skill
file requests is a step that gets skipped, and then API mode and session mode
print different check sheets, which is exactly the thing this table exists to
rule out.

Two surfaces over ONE list of rows — the screen and the CSV — so the count on
the screen and the line count in the file can never disagree.
"""
from __future__ import annotations

import csv
import io

from hawkeye.reports.quality_ja import _flag_ja
from hawkeye.scout.inspection import Inspection, InspectionRow

_STAGE_JA = {
    "gate_passed": "入口ゲート通過",
    "gate_reject": "入口ゲートで落選",
    "not_reached": "順位が下位で未取得",
    "held": "実績待ちで保留",
    "below_screen": "サプライズ足切り未通過",
}
_SOURCE_JA = {"whispers": "決算専門サイト", "calendar": "決算カレンダー"}
_GUIDANCE_JA = {"beat": "上振れ", "miss": "下振れ", "inline": "ほぼ一致",
                "unverified": "未検証", "absent": "開示なし"}
# §10 asks the guidance cell to carry the REASON when no comparison was made.
# Without it every unread, unreadable and unguided print reads as 開示なし —
# which is the one thing the three-kind split of these reasons exists to stop.
_GUIDANCE_CELL_JA = {
    "pending_extraction": "未読(AI待ち)",
    "no_guidance_in_source": "開示なし",
    "no_number_in_source": "数字なし",
    "open_ended_range": "上限なし",
    "guidance_scope_qualified": "但し書きで見送り",
    "quote_not_in_source": "⚠️読み取り失敗",
    "quoted_the_wrong_sentence": "⚠️読み取り失敗",
    "period_unreadable": "⚠️読み取り失敗",
    "period_not_next_quarter": "⚠️読み取り失敗",
    "extraction_call_failed": "⚠️呼び出し失敗",
    "no_forward_consensus_to_compare": "物差しなし",
    "no_full_year_consensus_to_compare": "物差しなし",
    "full_year_consensus_is_another_year": "物差しが別年度",
    "guidance_period_not_comparable": "期間が非対応",
}
# Guidance outcomes that are the routine state of the world rather than
# something to check. They stay in the table cell and in the CSV; they do not
# each earn a line in 「確認が必要な行」.
_ROUTINE_GUIDANCE = frozenset((
    "pending_extraction", "no_guidance_in_source", "no_number_in_source",
    "open_ended_range", "guided_", "against_", "on_eps", "on_revenue",
    # The flag a print carries when it has no guidance and no recorded reason
    # (hawkeye/scout/quality.py). Most companies guide nothing; there is
    # nothing here for the reader to act on.
    "guidance_not_published",
))
_VERDICT_JA = {"good_quarter": "良い決算", "mixed": "強弱まちまち",
               "weak": "弱い決算", "unverified": "判定不能"}

# The CSV's columns, in order. Japanese headers because the file is opened by
# the user in a spreadsheet, not by this program.
_CSV_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ティッカー", "ticker"),
    ("発表日", "event_date"),
    ("四半期ラベル", "fiscal_quarter"),
    ("到達段階", "stage"),
    ("数値の出所", "numbers_source"),
    ("決算専門サイトを使わなかった理由", "numbers_reason"),
    ("EPS実績", "eps_actual"),
    ("EPS予想", "eps_estimate"),
    ("EPSサプライズ率(自前計算)", "eps_surprise_pct"),
    ("EPSサプライズ率(提供元の公表値)", "eps_surprise_reported"),
    ("売上実績", "revenue_actual"),
    ("売上予想", "revenue_estimate"),
    ("売上サプライズ率(自前計算)", "revenue_surprise_pct"),
    ("カレンダーのEPS実績", "calendar_eps_actual"),
    ("カレンダーのEPS予想", "calendar_eps_estimate"),
    ("カレンダーが矛盾行を返した", "conflicting_calendar_rows"),
    ("囁き予想", "whisper"),
    ("囁き予想比", "whisper_beat_pct"),
    ("ガイダンス判定", "guidance_status"),
    ("ガイダンス比", "guidance_pct"),
    ("ガイダンスの注記", "guidance_flags"),
    ("ガイダンスの原文引用", "guidance_excerpt"),
    ("ガイダンスの読み手", "guidance_extractor"),
    ("ガイダンスの読み手のモデル", "guidance_extractor_model"),
    ("3本柱の判定", "verdict"),
    ("順位付けの点数", "score"),
)


def render_inspection_ja(inspection: Inspection) -> str:
    """The check sheet: what was retrieved for every name the feed was asked
    about, then the frequencies §10 requires counted."""
    counts = inspection.counts
    lines = ["## 📋 取得データ点検表(判断材料ではありません)",
             "",
             "この表は、**この回のデータが正しく取れているかを目視で確認する"
             "ための点検表**です。順位や採否とは無関係に、決算専門サイトに"
             "問い合わせた銘柄を全件載せています。"]
    if not inspection.rows:
        lines.append("")
        lines.append("該当なし(この回は決算専門サイトへの問い合わせが"
                     "1件もありませんでした)。")
        lines.append("")
        lines.append(_counts_ja(counts))
        return "\n".join(lines)

    lines += ["",
              "| ティッカー | 発表日 | 四半期 | 出所 | EPS実績 | EPS予想 | "
              "EPS率 | 売上率 | 囁き比 | ガイダンス | 到達段階 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in inspection.rows:
        lines.append(
            f"| {r.ticker} | {r.event_date} | {r.fiscal_quarter or '-'} "
            f"| {_SOURCE_JA.get(r.numbers_source, r.numbers_source)} "
            f"| {_num(r.eps_actual)} | {_num(r.eps_estimate)} "
            f"| {_pct(r.eps_surprise_pct)} | {_pct(r.revenue_surprise_pct)} "
            f"| {_pct(r.whisper_beat_pct)} "
            f"| {_guidance_cell(r)} "
            f"| {_STAGE_JA.get(r.stage, r.stage)} |")

    notes = [note for r in inspection.rows for note in _notes_ja(r)]
    if notes:
        lines += ["", "### 確認が必要な行", ""] + notes
    lines += ["", _counts_ja(counts)]
    return "\n".join(lines)


def _guidance_cell(r: InspectionRow) -> str:
    """The guidance leg in one cell, carrying the reason when it was not judged.

    A comparison that WAS made shows the percentage; anything else shows why
    not. The first live run printed 開示なし for nineteen prints whose sentence
    had merely not been read yet, which is precisely the confusion the
    three-kind split of guidance reasons exists to prevent.
    """
    if r.guidance_pct is not None:
        return f"{_GUIDANCE_JA.get(r.guidance_status, r.guidance_status)} " \
               f"{r.guidance_pct:+.1f}%"
    if not r.guidance_status:
        return "未取得(順位が下位)"
    for flag in r.guidance_flags:
        if flag in _GUIDANCE_CELL_JA:
            return _GUIDANCE_CELL_JA[flag]
    return _GUIDANCE_JA.get(r.guidance_status, r.guidance_status)


def _notes_ja(r: InspectionRow) -> list[str]:
    """Everything about one row that a bare table cell cannot carry.

    Only the rows with something to check appear here. A note per row would
    make the section as long as the table and hide the four lines that matter
    — which is exactly what happened on the first live run, where nineteen
    「まだ読み取っていない」 lines drowned three real retrieval failures.
    """
    out = []
    if r.numbers_reason:
        out.append(f"- **{r.ticker}**: 決算専門サイトの数字を使いませんでした — "
                   f"{_flag_ja(r.numbers_reason)}")
    if r.conflicting_calendar_rows:
        out.append(f"- **{r.ticker}**: 決算カレンダーが同じ決算に矛盾する行を"
                   f"返しました(実績 {_num(r.calendar_eps_actual)} / 予想 "
                   f"{_num(r.calendar_eps_estimate)})。最も保守的な読みを"
                   f"採用しています")
    if not r.fiscal_quarter:
        out.append(f"- **{r.ticker}**: 四半期ラベルを決められませんでした"
                   f"(取得元が年度・四半期を示していない)")
    if r.eps_estimate is None:
        out.append(f"- **{r.ticker}**: 今期のアナリスト予想が取得できていない"
                   f"ため、サプライズ率を計算できません")
    if r.guidance_excerpt:
        out.append(f'- **{r.ticker}**: ガイダンスの比較を見送りました。'
                   f'会社の原文: "{r.guidance_excerpt}"')
    for flag in r.guidance_flags:
        if flag.startswith(tuple(_ROUTINE_GUIDANCE)) or flag in _ROUTINE_GUIDANCE:
            continue
        out.append(f"- **{r.ticker}**: ガイダンス — {_flag_ja(flag)}")
    return out


def _counts_ja(counts) -> str:
    """The five frequencies §10 requires, printed at zero as well.

    A line that disappears when the count is zero reads as a check that was
    never run, and these are exactly the measurements whose absence nobody
    would notice.
    """
    # 誰がガイダンスを読んだか(§13.3)。行ごとの注記ではなく1行にまとめる —
    # 全行が同じ読み手なのが通常で、そのときに21行並べても情報は増えない。
    # 逆に、途中でモデルを変えた回はここに2行出るので、比較できない読み取りが
    # 混ざっていることが1行で分かる。
    if counts.readers:
        readers = "、".join(
            f"{_reader_ja(who)} {n}件" for who, n in sorted(counts.readers.items()))
    else:
        readers = "なし(この回はガイダンスを1件も読んでいません)"
    return "\n".join([
        "### 必ず数える項目(0件でも表示します)",
        "",
        f"- ガイダンスを読んだ読み手: {readers}",
        f"- ガイダンスが未読のまま: **{counts.guidance_unread}件** "
        f"(`hawkeye guidance queue` で処理します。セッションモードでは"
        f"走査の中からAIを呼べないため、これが通常の状態です)",
        f"- 決算専門サイトに問い合わせた: **{counts.asked}件** "
        f"(うち同サイトの数字で判定 {counts.answered_by_feed}件 / "
        f"カレンダーの数字に戻した {counts.fell_back_to_calendar}件)",
        f"- 決算カレンダーが矛盾する行を返した: **"
        f"{counts.conflicting_calendar_rows}件**",
        f"- 今期のアナリスト予想が取れなかった: **"
        f"{counts.no_consensus_this_quarter}件**",
        f"- ガイダンスに但し書きが付いていて比較を見送った: **"
        f"{counts.guidance_declined_scope_qualified}件**",
        f"- 提供元があとから数字を書き換えていた: **"
        f"{counts.revisions_seen}件**",
        f"- カレンダーにはあったが問い合わせていない: "
        f"{counts.calendar_only_not_asked}件 "
        f"(サプライズが足切りに届かない、または問い合わせ上限"
        f"`scout_max_whispers`に達したため。点検表には載せていません — "
        f"照合する数字が1つも無いためです)",
    ])


def inspection_csv(inspection: Inspection) -> str:
    """The same rows as the screen, every column, for a spreadsheet.

    One row per InspectionRow and nothing else, so the line count in the file
    always matches the row count on the screen.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([header for header, _ in _CSV_COLUMNS])
    for row in inspection.rows:
        writer.writerow([_csv_cell(attr, getattr(row, attr))
                         for _, attr in _CSV_COLUMNS])
    return buffer.getvalue()


# The columns whose stored value is an English identifier or an unrounded
# ratio. The screen translates and rounds these; the file has to as well —
# it is read by the same person, and a spreadsheet full of `gate_reject` and
# `502.7149321266968` is not a check sheet.
_CSV_TRANSLATED = {"stage": _STAGE_JA, "numbers_source": _SOURCE_JA}
_CSV_ROUNDED = frozenset((
    "eps_surprise_pct", "eps_surprise_reported", "revenue_surprise_pct",
    "whisper_beat_pct", "guidance_pct",
))


def _csv_cell(attr: str, value) -> str:
    if attr in _CSV_TRANSLATED and value is not None:
        return _CSV_TRANSLATED[attr].get(value, str(value))
    if attr == "numbers_reason" and value:
        return _flag_ja(value)
    if attr == "guidance_status" and value:
        return _GUIDANCE_JA.get(value, value)
    if attr == "guidance_flags" and value:
        return " / ".join(_flag_ja(flag) for flag in value)
    if attr in _CSV_ROUNDED and value is not None:
        return f"{round(value, 2):g}"
    return _cell(value)


def _reader_ja(who: str) -> str:
    """`agent/claude-opus-4-8` -> AI(claude-opus-4-8). The model name is kept
    verbatim: it is the only thing that says whether two runs' readings are
    comparable, so it must not be softened into 「AI」."""
    kind, _, model = who.partition("/")
    label = {"agent": "AI", "code": "コード(正規表現)"}.get(kind, kind)
    return f"{label}({model})" if model else label


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "はい" if value else "いいえ"
    if isinstance(value, tuple):
        return " / ".join(str(v) for v in value)
    return str(value)


def _num(value) -> str:
    return "-" if value is None else f"{value:g}"


def _pct(value) -> str:
    return "-" if value is None else f"{value:+.1f}%"
