"""Investigation staging for drop reviews (§5.2(3) [3]).

The measurement half (`drop_review.py`) answers "did this candidate run away
from us". This half answers "why", and that question is where a review
process most easily starts lying to itself: asked after the fact, any model
will produce a fluent cause. Three mechanisms hold it honest.

**The decision-date cutoff is code, not instruction.** The investigator is
handed the articles published up to and including the decision day and never
sees the later ones. Asking a model to ignore what it can read is the same
empty guarantee as asking the Bull not to peek at the attack report — the
boundary has to be the file it receives (invariant 4).

**The easy verdict carries the burden of proof.** "Unforeseeable" ends an
inquiry: nothing to fix, no revision to draft. So when the record shows the
moving story *was* public before we decided and merely was not in what we
collected, the verdict is overturned to `collection_gap` in code. Left to a
prompt, every awkward case would drift into the category that requires no
follow-up (invariant 3: code enforces what prompts request).

**The measurement is never re-opened.** `submit()` merges the investigation
into the frozen numbers and hands back one row; it cannot alter alpha or z.
A staging file on disk (`var/drops/`) holds the numbers between measuring
and investigating so an interrupted round resumes instead of re-measuring —
the same reason `cases/` exists for the tribunal.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field

from hawkeye import paths
from hawkeye.contracts.models import (
    DropReview,
    MissCategory,
    NewsItem,
    ProposedChange,
    new_id,
    utc_date,
)
from hawkeye.scout.drop_review import CheckpointResult, to_drop_review


class DropCase(BaseModel):
    """One measured outlier awaiting its investigation."""
    id: str = Field(default_factory=lambda: new_id("drc"))
    measurement: DropReview
    # Why it was dropped. Carried on the case rather than added to the stored
    # DropReview: the reason already lives in the screened_candidate /
    # recommendation row being reviewed, and duplicating a decision fact into
    # the outcome record is how the two start disagreeing.
    reject_reason: str = ""
    failed_gates: list[str] = Field(default_factory=list)
    # What our own record held when the call was made. Empty is meaningful
    # for enrichment-cap drops: "never looked", not "looked and found none".
    record_at_decision: list[NewsItem] = Field(default_factory=list)
    # Re-fetched now, then cut at the decision date.
    published_before_decision: list[NewsItem] = Field(default_factory=list)
    excluded_after_decision: int = 0
    # Published in time, absent from our record — the collection_gap evidence.
    missed: list[NewsItem] = Field(default_factory=list)


# --- the cutoff -------------------------------------------------------------

def published_before(items: list[NewsItem],
                     decision_date: date) -> tuple[list[NewsItem], int]:
    """Split re-fetched coverage at the decision day; returns (kept, dropped).

    An item with no publish date is dropped. We cannot show it predates the
    decision, and this whole split is an argument about which side carries
    the burden — an undated item counted as visible would let hindsight in
    through the one door we built this to close.
    """
    kept = [n for n in items
            if n.published_at is not None
            and utc_date(n.published_at) <= decision_date]
    return kept, len(items) - len(kept)


# --- what we had vs what was out there --------------------------------------

_WORD = re.compile(r"[a-z0-9]+")


def _fingerprint(item: NewsItem) -> str:
    """Headline reduced to its words, lowercased. Syndication republishes the
    same story under many URLs; keying on the URL alone would report a
    collection gap for practically every candidate and bury the real ones."""
    return " ".join(_WORD.findall(item.headline.lower()))


def missed_items(record: list[NewsItem],
                 refetched: list[NewsItem]) -> list[NewsItem]:
    """Coverage that existed in time but is absent from our own record."""
    ours = {_fingerprint(n) for n in record}
    ours |= {n.url for n in record if n.url}
    return [n for n in refetched
            if _fingerprint(n) not in ours and (not n.url or n.url not in ours)]


# --- opening a case ---------------------------------------------------------

def open_case(result: CheckpointResult,
              record_at_decision: list[NewsItem],
              refetched: list[NewsItem],
              reviewer_model: str = "") -> DropCase:
    """Freeze the measurement and stage the evidence for investigation."""
    visible, excluded = published_before(refetched, result.decision_date)
    return DropCase(
        measurement=to_drop_review(result, reviewer_model=reviewer_model),
        reject_reason=result.reject_reason,
        failed_gates=list(result.failed_gates),
        record_at_decision=list(record_at_decision),
        published_before_decision=visible,
        excluded_after_decision=excluded,
        missed=missed_items(record_at_decision, visible))


# --- the file queue ---------------------------------------------------------

def _case_path(case_id: str):
    return paths.drops_dir() / f"{case_id}.json"


def save_case(case: DropCase) -> None:
    paths.drops_dir().mkdir(parents=True, exist_ok=True)
    _case_path(case.id).write_text(case.model_dump_json(indent=2),
                                   encoding="utf-8")


def load_case(case_id: str) -> DropCase:
    return DropCase.model_validate_json(
        _case_path(case_id).read_text(encoding="utf-8"))


def list_cases() -> list[DropCase]:
    """Everything still awaiting investigation, oldest decision first.

    An unreadable file is reported rather than skipped in silence: a queue
    that quietly shrinks looks exactly like a queue that was worked through.
    """
    d = paths.drops_dir()
    if not d.exists():
        return []
    cases: list[DropCase] = []
    for p in sorted(d.glob("drc_*.json")):
        try:
            cases.append(DropCase.model_validate_json(
                p.read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"warning: unreadable drop case {p}: {exc}", file=sys.stderr)
    return sorted(cases, key=lambda c: c.measurement.decision_date)


def discard(case_id: str) -> bool:
    """Delete a staged case. Called only after its ledger write is confirmed,
    never before — the staging file is what makes a failed write retryable
    (the M5 ordering)."""
    p = _case_path(case_id)
    if not p.exists():
        return False
    p.unlink()
    return True


# --- what the investigator sees ---------------------------------------------

def _news_block(items: list[NewsItem], empty: str) -> str:
    if not items:
        return f"  （{empty}）"
    out = []
    for n in items:
        day = utc_date(n.published_at).isoformat() if n.published_at else "日付不明"
        out.append(f"  - [{day}] {n.headline}"
                   + (f" — {n.source}" if n.source else "")
                   + (f"\n    {n.url}" if n.url else "")
                   + (f"\n    {n.summary}" if n.summary else ""))
    return "\n".join(out)


def render_input(case: DropCase) -> str:
    """The investigation package — deliberately states the move and withholds
    any suggestion of a cause."""
    m = case.measurement
    direction = "上振れ" if m.direction == "up" else "下振れ"
    gates = "、".join(case.failed_gates) if case.failed_gates else "—"
    lines = [
        f"# 落選候補の調査: {m.ticker}",
        "",
        f"- 判断日: {m.decision_date}(この日に落選と判定した)",
        f"- 落選した段階: {m.cohort}",
        f"- 落選理由(記録されていたもの): {case.reject_reason or '—'}",
        f"- 不合格になったゲート: {gates}",
        f"- 観測点: {m.checkpoint}(判断日から{m.horizon_days}営業日後 = "
        f"{m.checkpoint_date})",
        f"- 値動き: 実測 {m.raw_return_pct:+.2f}% / "
        f"市場要因を除いた分(α) {m.alpha_pct:+.2f}% / "
        f"その銘柄自身の平常の値幅に対する倍率(z) {m.z:+.2f} → {direction}",
        "",
        "## ① 判断時点で我々の記録にあった材料",
        _news_block(case.record_at_decision,
                    "記録なし。この段階では材料を一度も取得していない可能性がある"),
        "",
        "## ② 判断日までに公開されていた記事(今あらためて取得したもの)",
        _news_block(case.published_before_decision, "該当なし"),
        "",
        f"※ 判断日より後に公開された記事 {case.excluded_after_decision}件 は、"
        "後知恵を避けるため機械的に除外してあり、ここには含まれません。",
        "",
        "## ②にあって①に無いもの(＝取りこぼし候補)",
        _news_block(case.missed, "該当なし"),
        "",
        "## 答えてほしいこと",
        "1. 何が起きたのか(`what_happened`)",
        "2. 判断時点の記録から引用できる根拠(`visible_evidence`)。"
        "当時見えていた材料を引用できない説明は物語であって分析ではありません。",
        "3. 分類(`miss_category`)。"
        "「判断日より後に発生・公開された情報が動かした」場合のみ `unforeseeable`。"
        "「判断日より前に公開されていたのに我々が持っていなかった」なら "
        "`collection_gap`。迷ったら `collection_gap` を選んでください。",
        "4. 絞り込みロジックをどう変えるべきか(`proposed_change`)と確信度"
        "(`confidence`, 0.0〜1.0)",
    ]
    return "\n".join(lines)


# --- submitting -------------------------------------------------------------

def _clamp(value, lo: float, hi: float) -> Optional[float]:
    if value is None:
        return None
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return None


def _parse_change(raw) -> Optional[ProposedChange]:
    if not isinstance(raw, dict):
        return None
    target = str(raw.get("target", "")).strip()
    direction = str(raw.get("direction", "")).strip()
    if not target or not direction:
        return None
    return ProposedChange(target=target, direction=direction,
                          rationale=str(raw.get("rationale", "")).strip())


_OVERRIDE_NOTE = (
    "[RULE ENFORCEMENT] 調査は unforeseeable と回答したが、値動きを説明しうる"
    "記事が判断日より前に公開されており我々の記録に無かったため "
    "collection_gap に訂正した(§5.2(3) 2026-08-01)。取りこぼし {n} 件: {heads}"
)


def submit(case: DropCase, reply: dict,
           reviewer_model: str = "") -> DropReview:
    """Merge one investigation into its frozen measurement.

    Raises on anything that would poison the tallies [4] acts on: an unknown
    category is a value nobody counts, and `other` without notes is a pile
    that can never be re-cut into real categories later.
    """
    raw_category = str(reply.get("miss_category", "")).strip()
    try:
        category = MissCategory(raw_category)
    except ValueError:
        raise ValueError(
            f"unknown miss_category {raw_category!r} — must be one of: "
            + ", ".join(c.value for c in MissCategory))

    notes = str(reply.get("notes", "")).strip()

    if category is MissCategory.UNFORESEEABLE and case.missed:
        heads = " / ".join(n.headline for n in case.missed[:3])
        override = _OVERRIDE_NOTE.format(n=len(case.missed), heads=heads)
        notes = f"{notes}\n{override}".strip() if notes else override
        category = MissCategory.COLLECTION_GAP

    if category is MissCategory.OTHER and not notes:
        raise ValueError("miss_category 'other' requires notes")

    evidence = [str(x).strip() for x in reply.get("visible_evidence", [])
                if str(x).strip()]
    urls = [str(x).strip() for x in reply.get("evidence_urls", [])
            if str(x).strip()]

    return case.measurement.model_copy(update={
        "what_happened": str(reply.get("what_happened", "")).strip(),
        "visible_evidence": evidence,
        "miss_category": category,
        "notes": notes,
        "evidence_urls": urls,
        "proposed_change": _parse_change(reply.get("proposed_change")),
        "confidence": _clamp(reply.get("confidence"), 0.0, 1.0),
        "reviewer_model": reviewer_model or case.measurement.reviewer_model,
    })


def load_reply(path: str) -> dict:
    raw = json.loads(open(path, encoding="utf-8").read())
    if not isinstance(raw, dict):
        raise ValueError("investigation reply must be a JSON object")
    return raw
