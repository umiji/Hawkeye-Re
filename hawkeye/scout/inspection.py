"""The inspection table: every name the earnings feed was asked about (§10).

This is a CHECK SHEET, not a judgment. It exists so the user can confirm, on
every scan, that the data behind the ranking was retrieved correctly — a
mislabelled quarter, a vendor switch, a guidance comparison declined for a
reason that turns out to be wrong. Nothing here feeds a decision.

The population is every name the feed was asked about, and that set is
recoverable from the events themselves: a name the feed answered for carries
`numbers_source == "whispers"`, a name it declined carries a named
`numbers_reason`, and a name nobody asked carries neither. Names that only
ever existed as a calendar row are deliberately NOT rows here — they have no
figure this table could check, and two thousand blank rows would bury the
fifty that can be checked (User decision, 2026-08-10).

Columns come from two places and the join is by (ticker, day): the numbers
from the event, the guidance reading and the three-leg verdict from the
candidate, which exists only for names the walk reached. A name asked about
but never enriched has no guidance columns, and that is a fact about the walk
rather than about the company — `stage` says which.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from hawkeye.scout.earnings import (
    EarningsEvent,
    eps_surprise_pct,
    revenue_surprise_pct,
)
from hawkeye.scout.quality import EarningsQuality, LegStatus

# Where a name ended up. Not a judgment of the company — a statement of how
# far this scan carried it, so a blank guidance column can be told apart from
# a company that published no outlook.
STAGE_TRIBUNAL = "gate_passed"      # cleared the entry gates; rankable
STAGE_GATE_REJECT = "gate_reject"   # enriched, then refused by the gates
STAGE_NOT_REACHED = "not_reached"   # the walk stopped before this name
STAGE_HELD = "held"                 # its own numbers had not arrived
STAGE_BELOW_SCREEN = "below_screen"  # asked, but the surprise did not clear


@dataclass(frozen=True)
class InspectionRow:
    ticker: str
    event_date: date
    fiscal_quarter: str
    stage: str
    # Which vendor the pair below came from, and why it is not the feed's.
    numbers_source: str = "calendar"
    numbers_reason: str = ""
    eps_actual: Optional[float] = None
    eps_estimate: Optional[float] = None
    eps_surprise_pct: Optional[float] = None
    # The surprise the vendor PUBLISHED, when it published one. Shown beside
    # our own computation and never used in place of it: the two differ
    # because vendors compute from full precision and display a rounded
    # estimate, and seeing both is how a reader tells that apart from a bug.
    eps_surprise_reported: Optional[float] = None
    revenue_actual: Optional[float] = None
    revenue_estimate: Optional[float] = None
    revenue_surprise_pct: Optional[float] = None
    # What the calendar said, kept beside what was used. A switch to the feed
    # is invisible without it.
    calendar_eps_actual: Optional[float] = None
    calendar_eps_estimate: Optional[float] = None
    conflicting_calendar_rows: bool = False
    whisper: Optional[float] = None
    whisper_beat_pct: Optional[float] = None
    # The guidance leg as it was judged, plus who read the sentence.
    guidance_status: str = ""
    guidance_pct: Optional[float] = None
    guidance_flags: tuple[str, ...] = ()
    guidance_excerpt: str = ""
    guidance_extractor: str = ""
    guidance_extractor_model: str = ""
    verdict: str = ""
    score: Optional[float] = None


@dataclass
class InspectionCounts:
    """The frequencies §10 requires counted on every scan.

    Each one is a way the data can be wrong that no single row reveals. They
    are printed even at zero: "0件" is the measurement, and a missing line
    reads as a check that was not run.
    """
    asked: int = 0
    answered_by_feed: int = 0
    fell_back_to_calendar: int = 0
    conflicting_calendar_rows: int = 0
    no_consensus_this_quarter: int = 0
    guidance_declined_scope_qualified: int = 0
    revisions_seen: int = 0
    calendar_only_not_asked: int = 0
    # Guidance the agent has not read yet (session mode stages the sentence and
    # a separate step reads it). Counted rather than listed per name: on a
    # session-mode scan this is EVERY row, and a note per row buried the four
    # lines that actually needed checking (found on the first live run).
    guidance_unread: int = 0
    # extractor/model -> how many prints it read. §13.3 wants this visible
    # because a guidance reading is only comparable with another produced by
    # the same reader.
    readers: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"inspection_asked": self.asked,
                "inspection_answered_by_feed": self.answered_by_feed,
                "inspection_fell_back": self.fell_back_to_calendar,
                "inspection_conflicting_rows": self.conflicting_calendar_rows,
                "inspection_no_consensus": self.no_consensus_this_quarter,
                "inspection_guidance_scope_qualified":
                    self.guidance_declined_scope_qualified,
                "inspection_revisions": self.revisions_seen,
                "inspection_not_asked": self.calendar_only_not_asked}


@dataclass
class Inspection:
    rows: list[InspectionRow] = field(default_factory=list)
    counts: InspectionCounts = field(default_factory=InspectionCounts)


def was_asked(event: EarningsEvent) -> bool:
    """Whether the feed was asked about this print.

    `numbers_reason` is set only when the feed answered unusably, and
    `numbers_source` becomes "whispers" only when it answered usably. Neither
    means nobody asked — which is why the two are read together rather than
    a third flag being added to the event.
    """
    return event.numbers_source == "whispers" or bool(event.numbers_reason)


def build_inspection(events: list[EarningsEvent], result,
                     revisions_seen: int = 0) -> Inspection:
    """One row per name the feed was asked about, newest surprise first."""
    stages = _stages(result)
    rows: list[InspectionRow] = []
    counts = InspectionCounts(revisions_seen=revisions_seen)
    for event in events:
        if not was_asked(event):
            counts.calendar_only_not_asked += 1
            continue
        counts.asked += 1
        if event.numbers_source == "whispers":
            counts.answered_by_feed += 1
        else:
            counts.fell_back_to_calendar += 1
        if event.conflicting_estimates:
            counts.conflicting_calendar_rows += 1
        if event.eps_estimate is None:
            counts.no_consensus_this_quarter += 1
        quality = stages.get((event.ticker, event.day), (None, None))[1]
        if quality is not None:
            if "guidance_scope_qualified" in quality.guidance.flags:
                counts.guidance_declined_scope_qualified += 1
            if "pending_extraction" in quality.guidance.flags:
                counts.guidance_unread += 1
            if quality.guidance_extractor:
                who = "/".join(x for x in (quality.guidance_extractor,
                                           quality.guidance_extractor_model)
                               if x)
                counts.readers[who] = counts.readers.get(who, 0) + 1
        rows.append(_row(event,
                         stages.get((event.ticker, event.day),
                                    (STAGE_BELOW_SCREEN, None))))
    rows.sort(key=lambda r: (r.eps_surprise_pct is None,
                             -(r.eps_surprise_pct or 0.0), r.ticker))
    return Inspection(rows=rows, counts=counts)


def _stages(result) -> dict[tuple[str, date], tuple[str, Optional[EarningsQuality]]]:
    """(ticker, day) -> how far the scan carried it, and its verdict.

    Built from the four lists the result already keeps, in an order where a
    later assignment cannot demote an earlier one — `passed` last, because a
    name that cleared the gates is the strongest statement available about it.
    """
    out: dict[tuple[str, date], tuple[str, Optional[EarningsQuality]]] = {}
    for stage, candidates in ((STAGE_HELD, getattr(result, "held", [])),
                              (STAGE_NOT_REACHED, getattr(result, "capped", [])),
                              (STAGE_GATE_REJECT, result.rejected),
                              (STAGE_TRIBUNAL, result.passed)):
        for c in candidates:
            out[(c.ticker, c.event_date)] = (stage, c.quality)
    return out


def _row(event: EarningsEvent,
         stage_and_quality: tuple[str, Optional[EarningsQuality]]
         ) -> InspectionRow:
    stage, quality = stage_and_quality
    guidance = quality.guidance if quality is not None else None
    return InspectionRow(
        ticker=event.ticker, event_date=event.day,
        fiscal_quarter=event.fiscal_quarter or "",
        stage=stage,
        numbers_source=event.numbers_source,
        numbers_reason=event.numbers_reason,
        eps_actual=event.eps_actual, eps_estimate=event.eps_estimate,
        eps_surprise_pct=eps_surprise_pct(event),
        eps_surprise_reported=event.eps_surprise_pct_reported,
        revenue_actual=event.revenue_actual,
        revenue_estimate=event.revenue_estimate,
        revenue_surprise_pct=revenue_surprise_pct(event),
        calendar_eps_actual=event.calendar_eps_actual,
        calendar_eps_estimate=event.calendar_eps_estimate,
        conflicting_calendar_rows=event.conflicting_estimates,
        whisper=event.whisper,
        whisper_beat_pct=(quality.whisper_beat_pct if quality else None),
        guidance_status=(guidance.status.value if guidance else ""),
        guidance_pct=(guidance.surprise_pct if guidance else None),
        guidance_flags=(guidance.flags if guidance else ()),
        guidance_excerpt=(guidance.excerpt if guidance else ""),
        guidance_extractor=(quality.guidance_extractor if quality else ""),
        guidance_extractor_model=(quality.guidance_extractor_model
                                  if quality else ""),
        verdict=(quality.verdict.value if quality else ""),
        score=(quality.score if quality else None))


def unread_guidance(rows: list[InspectionRow]) -> int:
    """Rows whose guidance was never read because the walk never reached them.

    Counted apart from a company that published nothing: the first is a budget
    boundary and the second is a fact about the company, and an empty guidance
    column looks the same either way.
    """
    return sum(1 for r in rows
               if not r.guidance_status
               or (r.stage in (STAGE_NOT_REACHED, STAGE_HELD)
                   and r.guidance_status == LegStatus.ABSENT.value))
