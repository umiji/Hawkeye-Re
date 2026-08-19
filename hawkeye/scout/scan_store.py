"""Persisting a scan across the guidance wait (docs/design/RANK_AFTER_GUIDANCE.ja.md).

`run_scout` judges every candidate before the guidance queue it just staged
can possibly be empty, so the `ScoreBreakdown` it leaves on each candidate is
provisional. `hawkeye scout` writes the whole `ScoutResult` here instead of
recording it to the ledger; `hawkeye rank` reads it back once
`hawkeye guidance queue` is empty, calls `rerank_after_guidance`, and only
THEN commits anything (invariant 1 — nothing here is ever partially recorded
and rewritten later).

`ScoutResult` mixes plain dataclasses with pydantic models (`CandidateBrief`,
`GateReport`, `ConsensusSnapshot`), so it cannot round-trip through a generic
`dataclasses.asdict()` — that would leave the pydantic instances unconverted
and `json.dumps` would fail on them. Every type that crosses this boundary is
therefore serialized by name, once, here.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Optional

from hawkeye.contracts.models import CandidateBrief, GateReport, ScoreBreakdown
from hawkeye.contracts.stocks import ConsensusSnapshot
from hawkeye.scout.guidance_agent import GuidanceStats
from hawkeye.scout.inspection import Inspection, InspectionCounts, InspectionRow
from hawkeye.scout.numbers import NumbersStats
from hawkeye.scout.quality import EarningsQuality, LegStatus, LegVerdict, QuarterVerdict
from hawkeye.scout.revision import Revision
from hawkeye.scout.scout import ScoutCandidate, ScoutResult


def save_scan_result(result: ScoutResult, path: Optional[Path] = None) -> Path:
    """Write a scan to disk, refusing to clobber one already waiting there.

    Refusing rather than overwriting is what stops a second `hawkeye scout`
    from silently discarding a scan that has not been ranked yet — the same
    reason `hawkeye guidance queue` works one case at a time.
    """
    from hawkeye import paths
    target = path or paths.scan_work_path()
    if target.exists():
        raise FileExistsError(
            f"a scan is already waiting to be ranked: {target} "
            "(run `hawkeye rank` first)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_result_to_dict(result), indent=2,
                                 ensure_ascii=False),
                      encoding="utf-8")
    return target


def load_scan_result(path: Optional[Path] = None) -> ScoutResult:
    from hawkeye import paths
    target = path or paths.scan_work_path()
    data = json.loads(target.read_text(encoding="utf-8"))
    return _result_from_dict(data)


def discard_scan_result(path: Optional[Path] = None) -> bool:
    """Remove the pending scan once `hawkeye rank` has recorded it."""
    from hawkeye import paths
    target = path or paths.scan_work_path()
    if not target.exists():
        return False
    target.unlink()
    return True


def has_pending_scan(path: Optional[Path] = None) -> bool:
    from hawkeye import paths
    return (path or paths.scan_work_path()).exists()


# --- ScoutResult -------------------------------------------------------------

def _result_to_dict(r: ScoutResult) -> dict:
    return {
        "scan_start": r.scan_start.isoformat(), "scan_end": r.scan_end.isoformat(),
        "scanned": r.scanned, "screened": r.screened, "enriched": r.enriched,
        "passed": [_candidate_to_dict(c) for c in r.passed],
        "rejected": [_candidate_to_dict(c) for c in r.rejected],
        "capped": [_candidate_to_dict(c) for c in r.capped],
        "held": [_candidate_to_dict(c) for c in r.held],
        "duplicates": r.duplicates, "window_truncated": r.window_truncated,
        "numbers": asdict(r.numbers),
        "enrichment_ceiling_hit": r.enrichment_ceiling_hit,
        "revisions": [asdict(rev) for rev in r.revisions],
        "guidance": asdict(r.guidance),
        "inspection": {
            "rows": [_inspection_row_to_dict(row) for row in r.inspection.rows],
            "counts": asdict(r.inspection.counts),
        },
    }


def _result_from_dict(d: dict) -> ScoutResult:
    return ScoutResult(
        scan_start=date.fromisoformat(d["scan_start"]),
        scan_end=date.fromisoformat(d["scan_end"]),
        scanned=d["scanned"], screened=d["screened"], enriched=d["enriched"],
        passed=[_candidate_from_dict(c) for c in d["passed"]],
        rejected=[_candidate_from_dict(c) for c in d["rejected"]],
        capped=[_candidate_from_dict(c) for c in d["capped"]],
        held=[_candidate_from_dict(c) for c in d["held"]],
        duplicates=d["duplicates"], window_truncated=d["window_truncated"],
        numbers=NumbersStats(**d["numbers"]),
        enrichment_ceiling_hit=d["enrichment_ceiling_hit"],
        revisions=[Revision(**rev) for rev in d["revisions"]],
        guidance=GuidanceStats(**d["guidance"]),
        inspection=Inspection(
            rows=[_inspection_row_from_dict(row)
                  for row in d["inspection"]["rows"]],
            counts=InspectionCounts(**d["inspection"]["counts"])),
    )


# --- ScoutCandidate ----------------------------------------------------------

def _candidate_to_dict(c: ScoutCandidate) -> dict:
    return {
        "ticker": c.ticker, "event_date": c.event_date.isoformat(),
        "eps_surprise_pct": c.eps_surprise_pct,
        "revenue_surprise_pct": c.revenue_surprise_pct,
        "score": c.score, "score_version": c.score_version,
        "eps_surprise_trusted": c.eps_surprise_trusted,
        "revenue_surprise_trusted": c.revenue_surprise_trusted,
        "conflicting_estimates": c.conflicting_estimates,
        "numbers_source": c.numbers_source, "numbers_reason": c.numbers_reason,
        "calendar_eps_surprise_pct": c.calendar_eps_surprise_pct,
        # How the company's own release was cut (T-013). The scan writes the
        # pending file and `hawkeye rank` reads it back hours later, so a
        # count missing from this pair reaches the ledger as a zero — which
        # the report renders as "the release was never read".
        "cause_blocks_kept": c.cause_blocks_kept,
        "cause_blocks_repaired": c.cause_blocks_repaired,
        "cause_blocks_altered": c.cause_blocks_altered,
        "cause_blocks_refused": c.cause_blocks_refused,
        "held_reason": c.held_reason, "held_expired": c.held_expired,
        "price": c.price,
        "price_asof": c.price_asof.isoformat() if c.price_asof else None,
        "brief": c.brief.model_dump(mode="json") if c.brief is not None else None,
        "gate_report": (c.gate_report.model_dump(mode="json")
                        if c.gate_report is not None else None),
        "reject_reason": c.reject_reason,
        "quality": _quality_to_dict(c.quality),
        "stock_id": c.stock_id,
        "consensus": (c.consensus.model_dump(mode="json")
                     if c.consensus is not None else None),
    }


def _candidate_from_dict(d: dict) -> ScoutCandidate:
    return ScoutCandidate(
        ticker=d["ticker"], event_date=date.fromisoformat(d["event_date"]),
        eps_surprise_pct=d["eps_surprise_pct"],
        revenue_surprise_pct=d["revenue_surprise_pct"],
        score=d["score"], score_version=d["score_version"],
        eps_surprise_trusted=d["eps_surprise_trusted"],
        revenue_surprise_trusted=d["revenue_surprise_trusted"],
        conflicting_estimates=d["conflicting_estimates"],
        numbers_source=d["numbers_source"], numbers_reason=d["numbers_reason"],
        calendar_eps_surprise_pct=d["calendar_eps_surprise_pct"],
        # `.get` with a zero default: a pending file written before T-013 has
        # no counts, and zero there is the truth — that scan never read a
        # release with this step in it.
        cause_blocks_kept=d.get("cause_blocks_kept", 0),
        cause_blocks_repaired=d.get("cause_blocks_repaired", 0),
        cause_blocks_altered=d.get("cause_blocks_altered", 0),
        cause_blocks_refused=d.get("cause_blocks_refused", 0),
        held_reason=d["held_reason"], held_expired=d["held_expired"],
        price=d["price"],
        price_asof=(date.fromisoformat(d["price_asof"])
                    if d["price_asof"] else None),
        brief=(CandidateBrief.model_validate(d["brief"])
              if d["brief"] is not None else None),
        gate_report=(GateReport.model_validate(d["gate_report"])
                    if d["gate_report"] is not None else None),
        reject_reason=d["reject_reason"],
        quality=_quality_from_dict(d["quality"]),
        stock_id=d.get("stock_id", ""),
        consensus=(ConsensusSnapshot.model_validate(d["consensus"])
                  if d.get("consensus") is not None else None),
    )


# --- EarningsQuality / LegVerdict ---------------------------------------------

def _leg_to_dict(lv: LegVerdict) -> dict:
    return {"leg": lv.leg, "status": lv.status.value,
            "surprise_pct": lv.surprise_pct, "actual": lv.actual,
            "estimate": lv.estimate, "source": lv.source,
            "other_actual": lv.other_actual, "analysts": lv.analysts,
            "flags": list(lv.flags),
            "parts": [list(p) for p in lv.parts],
            "excerpt": lv.excerpt}


def _leg_from_dict(d: dict) -> LegVerdict:
    return LegVerdict(leg=d["leg"], status=LegStatus(d["status"]),
                      surprise_pct=d["surprise_pct"], actual=d["actual"],
                      estimate=d["estimate"], source=d["source"],
                      other_actual=d["other_actual"], analysts=d["analysts"],
                      flags=tuple(d["flags"]),
                      parts=tuple(tuple(p) for p in d["parts"]),
                      excerpt=d["excerpt"])


def _quality_to_dict(q: Optional[EarningsQuality]) -> Optional[dict]:
    if q is None:
        return None
    return {
        "ticker": q.ticker, "fiscal_quarter": q.fiscal_quarter,
        "eps": _leg_to_dict(q.eps), "revenue": _leg_to_dict(q.revenue),
        "guidance": _leg_to_dict(q.guidance), "verdict": q.verdict.value,
        "score": q.score,
        "breakdown": (q.breakdown.model_dump(mode="json")
                     if q.breakdown is not None else None),
        "flags": list(q.flags), "whisper": q.whisper,
        "whisper_beat_pct": q.whisper_beat_pct,
        "guidance_extractor": q.guidance_extractor,
        "guidance_extractor_model": q.guidance_extractor_model,
    }


def _quality_from_dict(d: Optional[dict]) -> Optional[EarningsQuality]:
    if d is None:
        return None
    return EarningsQuality(
        ticker=d["ticker"], fiscal_quarter=d["fiscal_quarter"],
        eps=_leg_from_dict(d["eps"]), revenue=_leg_from_dict(d["revenue"]),
        guidance=_leg_from_dict(d["guidance"]),
        verdict=QuarterVerdict(d["verdict"]), score=d["score"],
        breakdown=(ScoreBreakdown.model_validate(d["breakdown"])
                  if d["breakdown"] is not None else None),
        flags=tuple(d["flags"]), whisper=d["whisper"],
        whisper_beat_pct=d["whisper_beat_pct"],
        guidance_extractor=d["guidance_extractor"],
        guidance_extractor_model=d["guidance_extractor_model"])


# --- InspectionRow (the only Inspection member with non-primitive fields) ----

def _inspection_row_to_dict(row: InspectionRow) -> dict:
    d = asdict(row)
    d["event_date"] = row.event_date.isoformat()
    d["guidance_flags"] = list(row.guidance_flags)
    return d


def _inspection_row_from_dict(d: dict) -> InspectionRow:
    d = dict(d)
    d["event_date"] = date.fromisoformat(d["event_date"])
    d["guidance_flags"] = tuple(d["guidance_flags"])
    return InspectionRow(**d)
