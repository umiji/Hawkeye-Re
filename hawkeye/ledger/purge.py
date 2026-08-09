"""Which held rows are safe to delete, and which are not (task 6.5).

Selection only. Nothing here touches the database — the write, and the journal
event that keeps the hash chain honest about it, live in
`Ledger.purge_screened_candidates`.

The split matters: the caller has to be able to SHOW the user what would be
removed, and why each survivor was kept, before anything is destroyed. A
command whose dry run and whose real run compute their answers separately is a
command whose preview can lie.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

from hawkeye.contracts.models import (
    DropReview,
    ScreenedCandidate,
    ScreenedCandidateStage,
)

# Why a row was kept. Machine-readable; the Japanese wording lives in the
# report layer, the same rule the leg verdicts follow.
STILL_WAITING = "still_waiting"
TIMEOUT_IS_A_MEASUREMENT = "timeout_is_a_measurement"
REFERENCED_BY_DROP_REVIEW = "referenced_by_drop_review"
NOT_A_HOLD = "not_a_hold"
OUTSIDE_WINDOW = "outside_window"


@dataclass(frozen=True)
class PurgePlan:
    """What a purge would do. `protected` carries the reason per row so the
    user is told what was spared rather than left to infer it from a count."""
    removable: list[ScreenedCandidate] = field(default_factory=list)
    protected: list[tuple[ScreenedCandidate, str]] = field(default_factory=list)

    @property
    def protected_by(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, reason in self.protected:
            counts[reason] = counts.get(reason, 0) + 1
        return counts


def _settled(candidate: ScreenedCandidate,
             by_print: dict[tuple[str, date], list[ScreenedCandidate]]) -> bool:
    """Whether the print this row was waiting on has stopped being open.

    Settled means the same (ticker, event date) reached ANY stage past the
    hold — judged, gate-rejected, capped out, or given up on. One rule covers
    all of them because they share the only property that matters here: the
    scan is no longer coming back to this print, so a row saying "waiting" is
    describing a wait that ended.
    """
    return any(other.stage is not ScreenedCandidateStage.ACTUAL_PENDING
               for other in by_print.get((candidate.ticker,
                                          candidate.event_date), []))


def plan_purge(candidates: Iterable[ScreenedCandidate],
               reviews: Iterable[DropReview],
               before: Optional[date] = None,
               ticker: Optional[str] = None) -> PurgePlan:
    """Sort `candidates` into what may be deleted and what may not.

    `before` bounds the window by the date the row was RECORDED, not by the
    print's date: the row is what is being deleted, and "delete everything I
    wrote before last month" is the question an operator actually has.
    """
    rows = list(candidates)
    reviewed = {r.screened_candidate_id for r in reviews
                if r.screened_candidate_id}
    by_print: dict[tuple[str, date], list[ScreenedCandidate]] = {}
    for row in rows:
        by_print.setdefault((row.ticker, row.event_date), []).append(row)

    removable: list[ScreenedCandidate] = []
    protected: list[tuple[ScreenedCandidate, str]] = []
    for row in rows:
        if ticker is not None and row.ticker != ticker:
            continue
        if row.stage is ScreenedCandidateStage.ACTUAL_TIMEOUT:
            protected.append((row, TIMEOUT_IS_A_MEASUREMENT))
            continue
        if row.stage is not ScreenedCandidateStage.ACTUAL_PENDING:
            # Not in scope for this command at all, so it is not reported as
            # something that was spared — saying "protected: 400 rows" about
            # rows nobody asked to delete would bury the three that matter.
            continue
        if before is not None and row.recorded_at.date() >= before:
            continue
        if row.id in reviewed:
            protected.append((row, REFERENCED_BY_DROP_REVIEW))
            continue
        if not _settled(row, by_print):
            protected.append((row, STILL_WAITING))
            continue
        removable.append(row)
    return PurgePlan(removable=removable, protected=protected)
