"""One round of the drop-candidate review (§5.2(3) [2][3][4]).

Splits a batch of measurements into "record it now" and "this one needs a
name-by-name look", and decides whether enough of the same cause has piled
up to justify drafting a revision. Kept apart from the CLI so the rules that
decide what gets investigated are testable without a network or a ledger.

Two rules here are load-bearing and neither is a tunable:

**Only T+10 is investigated.** T+5 is measured and filed. Halving the
investigation cost is the smaller reason; the real one is that a name looked
at twice would land in the 20-per-category tally twice, and a revision
authorized by double-counted evidence is worse than no revision.

**Every measured candidate is recorded, not just the outliers.** Without the
denominator, "we found 3 names the screen got wrong" cannot be read as good
or bad. Beta and the split-adjusted prices behind it are also unrecoverable
later, so a measurement not written down is gone for good.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from hawkeye import paths
from hawkeye.contracts.models import DropReview, MissCategory
from hawkeye.scout.drop_review import (
    INVESTIGATION_COHORTS,
    Z_THRESHOLD,
    CheckpointResult,
)

# (screened_candidate_id, rec_id, checkpoint) — the subject key the ledger's
# unique index is built on. Absent ids are the empty string, never None:
# SQLite treats every NULL in a unique index as distinct, so a NULL rec_id
# would let the same subject be re-scored without the constraint firing.
SubjectKey = tuple[str, str, str]


def subject_key(result: CheckpointResult) -> SubjectKey:
    return (result.screened_candidate_id or "", result.rec_id or "",
            result.checkpoint)


@dataclass(frozen=True)
class RoundPlan:
    record_now: list[CheckpointResult] = field(default_factory=list)
    investigate: list[CheckpointResult] = field(default_factory=list)
    skipped_already_recorded: int = 0
    unmeasurable: int = 0


def plan(results: list[CheckpointResult], checkpoint: str,
         already_recorded: set[SubjectKey] | None = None,
         z_threshold: float = Z_THRESHOLD) -> RoundPlan:
    """Sort one checkpoint's measurements into the two piles."""
    seen = already_recorded or set()
    record_now: list[CheckpointResult] = []
    investigate: list[CheckpointResult] = []
    skipped = 0
    unmeasurable = 0

    for r in results:
        if subject_key(r) in seen:
            skipped += 1
            continue
        if r.alpha_pct is None or r.z is None or r.direction is None:
            unmeasurable += 1
            continue
        needs_look = (checkpoint == "t10"
                      and abs(r.z) >= z_threshold
                      and r.cohort in INVESTIGATION_COHORTS)
        (investigate if needs_look else record_now).append(r)

    return RoundPlan(record_now=record_now, investigate=investigate,
                     skipped_already_recorded=skipped,
                     unmeasurable=unmeasurable)


# --- the revision gate ------------------------------------------------------

def category_counts(reviews: list[DropReview]) -> dict[str, int]:
    """How many investigated reviews landed on each cause.

    Reviews with no category are measurements nobody explained; they belong
    in the denominator of the round report but not in an argument for
    changing a rule.
    """
    counts: dict[str, int] = {}
    for r in reviews:
        if r.miss_category is not None:
            key = r.miss_category.value
            counts[key] = counts.get(key, 0) + 1
    return counts


def ready_categories(reviews: list[DropReview],
                     min_samples: int) -> list[str]:
    """Causes seen enough times to name a knob (§5.2(3) [4])."""
    counts = category_counts(reviews)
    return sorted(k for k, n in counts.items() if n >= min_samples)


def remaining_to_threshold(reviews: list[DropReview],
                           min_samples: int) -> dict[str, int]:
    """Distance to the bar for every category, including the untouched ones.

    Reported whether or not anything is ready: a round that found nothing is
    a real result, and it only reads as one next to how far off the piles are.
    """
    counts = category_counts(reviews)
    return {c.value: max(0, min_samples - counts.get(c.value, 0))
            for c in MissCategory}


# --- round state ------------------------------------------------------------

class RoundState(BaseModel):
    """What one review round has done so far.

    Facts the ledger cannot answer live here: how many candidates were still
    inside their observation window, and how many could not be measured at
    all. Both are needed for the round report to be honest — a run that
    measured 3 of 200 reads very differently from one that measured 3 of 3 —
    and neither leaves a row behind, because there is nothing to record.

    Kept on disk so a round survives being interrupted between measuring and
    reporting, the same reason the investigation queue is a directory of
    files rather than a list in memory.
    """
    checkpoint: str = ""
    recorded_ids: list[str] = Field(default_factory=list)
    queued_case_ids: list[str] = Field(default_factory=list)
    pending: int = 0
    skipped_already_recorded: int = 0
    unmeasurable: int = 0
    censored: dict[str, int] = Field(default_factory=dict)
    total_before: int = 0


def _round_path():
    return paths.drops_dir() / "round.json"


def load_round() -> RoundState:
    p = _round_path()
    if not p.exists():
        return RoundState()
    try:
        return RoundState.model_validate_json(p.read_text(encoding="utf-8"))
    except Exception:
        return RoundState()


def save_round(state: RoundState) -> None:
    paths.drops_dir().mkdir(parents=True, exist_ok=True)
    _round_path().write_text(state.model_dump_json(indent=2), encoding="utf-8")


def clear_round() -> None:
    p = _round_path()
    if p.exists():
        p.unlink()


def merge_round(previous: RoundState, checkpoint: str, plan: "RoundPlan",
                recorded_ids: list[str], queued_case_ids: list[str],
                pending: int, censored: dict[str, int],
                total_before: int) -> RoundState:
    """Accumulate a measure pass into the round.

    Both checkpoints run in one round (T+5 files, T+10 may queue), so the
    second pass adds to the first rather than replacing it — otherwise the
    report would silently describe only half of what was measured.
    """
    merged_censored = dict(previous.censored)
    for cohort, count in censored.items():
        merged_censored[cohort] = merged_censored.get(cohort, 0) + count
    return RoundState(
        checkpoint=checkpoint,
        recorded_ids=previous.recorded_ids + recorded_ids,
        queued_case_ids=previous.queued_case_ids + queued_case_ids,
        pending=previous.pending + pending,
        skipped_already_recorded=(previous.skipped_already_recorded
                                  + plan.skipped_already_recorded),
        unmeasurable=previous.unmeasurable + plan.unmeasurable,
        censored=merged_censored,
        total_before=previous.total_before or total_before)
