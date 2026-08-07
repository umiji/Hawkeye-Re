"""Judging one quarter on three legs: EPS, revenue, guidance (§5.3).

The question this module answers is "was this a good quarter", and the
answer it is allowed to give is deliberately narrow. Two sources — Yahoo's
pre-registered distribution and Finnhub's point estimate — are compared, and
a leg counts as a beat only when BOTH of them say so. Where they disagree
the conservative reading is what gets ranked, extending across vendors the
collapse-to-conservative rule already applied to Finnhub's own duplicate rows
on 2026-08-01.

Three failure modes are handled apart from each other, because they are
resolvable by different things:

- **The actuals disagree** (AAPL 2.02 vs 1.91). Resolvable by a document:
  the company published both figures and said which is which. Until it is
  read, the leg is unverified.
- **The consensus disagrees** (BIIB 2.15 vs 3.98). Resolvable by nothing —
  no primary source for consensus exists anywhere — so it is flagged, both
  values are kept, and the conservative one ranks.
- **The consensus is too thin or too small** (INVH's single analyst; a REIT's
  near-zero GAAP consensus). Not a disagreement at all; the denominator just
  cannot carry a percentage.

Nothing here computes a non-GAAP figure. Non-GAAP is READ from the release
or it does not exist, and a quarter whose one-off nobody quantified travels
onward labelled `unadjusted` — a known unknown for the tribunal, not a
silent pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from hawkeye.contracts.models import now
from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    EarningsPrint,
    EpsBasis,
    PrintDepth,
    SnapshotKind,
)
from hawkeye.scout.earnings import (
    EarningsEvent,
    eps_points,
    revenue_points,
    score_candidate,
)


class LegStatus(str, Enum):
    BEAT = "beat"                # both sources say above consensus
    MISS = "miss"                # both sources say below
    INLINE = "inline"            # the sources disagree, or it landed on it
    UNVERIFIED = "unverified"    # cannot be confirmed; scores nothing
    ABSENT = "absent"            # nothing published (guidance's normal case)


class QuarterVerdict(str, Enum):
    GOOD_QUARTER = "good_quarter"
    MIXED = "mixed"
    WEAK = "weak"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class LegVerdict:
    """One leg's reading, with every input that produced it kept visible.

    `surprise_pct` is always the conservative reading. The per-source figures
    stay alongside it so a later review can measure how often, and how far,
    the vendors disagreed — the disagreement rate is data, not noise.
    """
    leg: str
    status: LegStatus
    surprise_pct: Optional[float] = None
    yahoo_surprise_pct: Optional[float] = None
    finnhub_surprise_pct: Optional[float] = None
    actual: Optional[float] = None
    # Each vendor's own actual, kept apart from the resolved one. Since a
    # disputed leg can now be scored (2026-08-03(b)), the two figures have to
    # remain readable — they are what the disagreement IS, and both the
    # tribunal's brief and the Japanese report name them.
    actual_yahoo: Optional[float] = None
    actual_finnhub: Optional[float] = None
    sources: int = 0                       # consensus sources compared
    analysts: Optional[int] = None
    basis: Optional[EpsBasis] = None
    # Machine-readable reasons, never prose. The Japanese wording lives in
    # `hawkeye/reports/quality_ja.py` and the English wording in
    # `describe_quality_en` — a verdict that carried its own sentence would
    # decide the reader's language here, in the wrong layer.
    flags: tuple[str, ...] = ()

    @property
    def scored_pct(self) -> Optional[float]:
        """What ranking may use: a confirmed beat, or nothing."""
        return self.surprise_pct if self.status is LegStatus.BEAT else None


@dataclass(frozen=True)
class EarningsQuality:
    ticker: str
    fiscal_quarter: str
    eps: LegVerdict
    revenue: LegVerdict
    guidance: LegVerdict
    verdict: QuarterVerdict
    score: float
    flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def legs(self) -> tuple[LegVerdict, LegVerdict, LegVerdict]:
        return (self.eps, self.revenue, self.guidance)


def _pct(actual: Optional[float], estimate: Optional[float]) -> Optional[float]:
    """A surprise percentage, rounded to four places.

    Rounded because these values are stored and compared: an unrounded ratio
    carries binary noise in its last digits, which turns "the two sources
    read the same" into a difference that only exists in the float.
    """
    if actual is None or estimate is None or estimate == 0:
        return None
    return round((actual - estimate) / abs(estimate) * 100.0, 4)


def _relative_gap(a: float, b: float) -> float:
    scale = max(abs(a), abs(b))
    return abs(a - b) / scale * 100.0 if scale else 0.0


def _resolve_actual(yahoo: Optional[float], finnhub: Optional[float],
                    settled: Optional[float], config,
                    absolute_floor: Optional[float]
                    ) -> tuple[Optional[float], list[str]]:
    """The actual to judge on, and what was wrong with getting there.

    A settled figure (read from the company's own release) always wins: it is
    the only reading with a primary source behind it. Otherwise the two
    vendors must agree — and "agree" is two conditions, not one, because on a
    $0.30 EPS a single cent of rounding is 3%.
    """
    flags: list[str] = []
    if settled is not None:
        return settled, flags
    present = [v for v in (yahoo, finnhub) if v is not None]
    if not present:
        return None, ["no_actual"]
    if len(present) == 1:
        return present[0], ["single_source_actual"]
    gap_pct = _relative_gap(yahoo, finnhub)
    gap_abs = abs(yahoo - finnhub)
    disputed = gap_pct > config.earnings_actual_dispute_pct and (
        absolute_floor is None or gap_abs > absolute_floor)
    if disputed:
        flags.append("actual_disputed")
    # Conservative even when they agree: the smaller reading is the one that
    # cannot overstate the beat, and picking "the one that looks right" is
    # the judgment this system exists to remove.
    return min(present), flags


def _assess_leg(leg: str, actual: Optional[float], actual_flags: list[str],
                yahoo_estimate: Optional[float],
                finnhub_estimate: Optional[float],
                analysts: Optional[int], config,
                min_abs_estimate: Optional[float] = None,
                basis: Optional[EpsBasis] = None,
                actual_yahoo: Optional[float] = None,
                actual_finnhub: Optional[float] = None) -> LegVerdict:
    flags = list(actual_flags)
    estimates = [v for v in (yahoo_estimate, finnhub_estimate) if v is not None]
    yahoo_pct = _pct(actual, yahoo_estimate)
    finnhub_pct = _pct(actual, finnhub_estimate)
    readings = [p for p in (yahoo_pct, finnhub_pct) if p is not None]
    conservative = min(readings) if readings else None

    if len(estimates) == 2 and _relative_gap(*estimates) > \
            config.earnings_consensus_dispute_pct:
        flags.append("consensus_disputed")
    if len(estimates) == 1:
        flags.append("single_source_consensus")
    if analysts is not None and analysts < config.earnings_min_analysts:
        flags.append("thin_coverage")
    if (min_abs_estimate is not None and estimates
            and min(abs(v) for v in estimates) < min_abs_estimate):
        flags.append("estimate_too_small")
    if len(readings) == 2 and (readings[0] > 0) != (readings[1] > 0):
        flags.append("sources_disagree_on_direction")

    status = _leg_status(readings, estimates, flags, _blocking(config))
    if status is LegStatus.MISS and "actual_disputed" in flags:
        status = _disputed_miss_status(actual_yahoo, actual_finnhub,
                                       estimates)
    return LegVerdict(leg=leg, status=status, surprise_pct=conservative,
                      yahoo_surprise_pct=yahoo_pct,
                      finnhub_surprise_pct=finnhub_pct, actual=actual,
                      actual_yahoo=actual_yahoo, actual_finnhub=actual_finnhub,
                      sources=len(estimates), analysts=analysts, basis=basis,
                      flags=tuple(flags))


_BLOCKING_FLAGS = ("no_actual", "thin_coverage", "estimate_too_small")


def _disputed_miss_status(actual_yahoo: Optional[float],
                          actual_finnhub: Optional[float],
                          estimates: list[float]) -> LegStatus:
    """Whether a MISS read off the conservative actual is actually provable.

    A beat and a miss are not symmetric here, and the asymmetry is the whole
    reason a disputed leg may be scored at all. The leg is judged on the
    SMALLER actual, so:

    - a beat under it is a beat under the larger one too — established;
    - a miss under it says nothing about the larger one, which may well be a
      beat. AAPL's 2.02 and 1.91 differ by a one-off; a company that took a
      CHARGE instead shows the same gap the other way round.

    So a miss has to hold for the LARGER actual as well. Otherwise the leg
    goes back to unverified — scoring zero rather than subtracting, because
    our inability to tell which figure is comparable must never read as the
    company having done badly (invariant 6).
    """
    actuals = [v for v in (actual_yahoo, actual_finnhub) if v is not None]
    if len(actuals) < 2 or not estimates:
        return LegStatus.UNVERIFIED
    readings = [p for p in (_pct(max(actuals), e) for e in estimates)
                if p is not None]
    if readings and max(readings) < 0:
        return LegStatus.MISS
    return LegStatus.UNVERIFIED


def _blocking(config) -> tuple[str, ...]:
    """Which flags make a leg unverified, under the doctrine in force.

    Two of them are switches, and both were turned off by measurement rather
    than by preference:

    - a disputed ACTUAL stopped blocking on 2026-08-03(b) — the reading is
      already the smaller actual against the larger consensus, so a beat under
      dispute holds under either vendor's numbers;
    - a SINGLE-SOURCE consensus stopped blocking on 2026-08-05, when
      EarningsWhispers became the one source of earnings numbers. Every
      consensus is single-source now, so the rule had stopped selecting
      anything and had become an unconditional veto.

    See `config.earnings_actual_dispute_blocks` and
    `config.earnings_single_source_consensus_blocks` for the full rationale;
    this function exists so each doctrine change is one config diff and not an
    edit scattered through the judgment.
    """
    blocking = _BLOCKING_FLAGS
    if getattr(config, "earnings_actual_dispute_blocks", False):
        blocking += ("actual_disputed",)
    if getattr(config, "earnings_single_source_consensus_blocks", True):
        blocking += ("single_source_consensus",)
    return blocking


def _leg_status(readings: list[float], estimates: list[float],
                flags: list[str], blocking: tuple[str, ...]) -> LegStatus:
    """Unverified beats every other reading.

    A leg that cannot be confirmed must not be scored as a beat OR written
    off as a miss: both would be claims the data does not support. It scores
    zero and travels onward saying so (invariant 6).
    """
    if not estimates and not readings:
        return LegStatus.ABSENT if not flags else LegStatus.UNVERIFIED
    if any(flag in blocking for flag in flags) or not readings:
        return LegStatus.UNVERIFIED
    if min(readings) > 0:
        return LegStatus.BEAT
    if max(readings) < 0:
        return LegStatus.MISS
    return LegStatus.INLINE


def _next_quarter(fiscal_quarter: str) -> str:
    """`2026-Q2` -> `2026-Q3`; the quarter guidance is supposed to be about.

    Empty when the label is not one this system produced, which makes the
    comparison below refuse rather than guess.
    """
    year, _, quarter = (fiscal_quarter or "").partition("-Q")
    if not quarter.isdigit() or not year.isdigit():
        return ""
    number = int(quarter)
    return f"{int(year) + 1}-Q1" if number == 4 else f"{year}-Q{number + 1}"


def _guidance_leg(print_row: EarningsPrint,
                  consensus: Optional[ConsensusSnapshot]) -> LegVerdict:
    """Guidance against next quarter's consensus, captured at the same moment.

    Absence is neutral by design and is the normal case: there is no
    structured source for guidance on any free tier, so penalising its
    absence would quietly penalise the data gap rather than the company.
    """
    guidance = print_row.guidance
    # The only yardstick captured anywhere in this system is NEXT QUARTER's
    # consensus, so guidance for any other period has nothing here to be
    # measured against. Left unchecked this is not a small error: ADM guided
    # FY2026 EPS of $5.15-$5.60 against a quarterly consensus near $1.20, and
    # the comparison came out as a +348% guidance beat the company never gave.
    # An unlabelled reading keeps its old meaning (next quarter) so that the
    # readings recorded before the label existed still count.
    if guidance is not None and guidance.period and (
            guidance.period != _next_quarter(print_row.fiscal_quarter)):
        return LegVerdict(leg="guidance", status=LegStatus.ABSENT,
                          flags=("guidance_period_not_comparable",
                                 f"guided_{guidance.period}"))
    # EPS first, revenue second. Plenty of companies guide only on sales —
    # Amazon gives net sales and operating income and never an EPS range —
    # and scoring those as "no guidance" would describe our reading rather
    # than the company.
    pairs = (((guidance.eps_midpoint if guidance else None),
              (consensus.next_quarter_eps_avg if consensus else None),
              "eps"),
             ((guidance.revenue_midpoint if guidance else None),
              (consensus.next_quarter_revenue_avg if consensus else None),
              "revenue"))
    usable = [(value, yardstick, unit) for value, yardstick, unit in pairs
              if value is not None and yardstick is not None]
    if guidance is None or not usable:
        flag = ("guidance_not_published" if guidance is None
                else "no_forward_consensus_to_compare")
        return LegVerdict(leg="guidance", status=LegStatus.ABSENT,
                          flags=(flag,))
    midpoint, yardstick, unit = usable[0]
    surprise = _pct(midpoint, yardstick)
    status = (LegStatus.BEAT if surprise and surprise > 0
              else LegStatus.MISS if surprise and surprise < 0
              else LegStatus.INLINE)
    return LegVerdict(leg="guidance", status=status, surprise_pct=surprise,
                      actual=midpoint, sources=1, flags=(f"on_{unit}",))


def _verdict(eps: LegVerdict, revenue: LegVerdict,
             guidance: LegVerdict) -> QuarterVerdict:
    if eps.status is LegStatus.UNVERIFIED:
        return QuarterVerdict.UNVERIFIED
    if LegStatus.MISS in (eps.status, revenue.status):
        return QuarterVerdict.WEAK
    if (eps.status is LegStatus.BEAT and revenue.status is LegStatus.BEAT
            and guidance.status is not LegStatus.MISS):
        return QuarterVerdict.GOOD_QUARTER
    return QuarterVerdict.MIXED


_EN_STATUS = {LegStatus.BEAT: "beat", LegStatus.MISS: "missed",
              LegStatus.INLINE: "in line with", LegStatus.UNVERIFIED:
              "UNVERIFIED against", LegStatus.ABSENT: "not disclosed —"}


_EN_LEG = {"eps": "EPS", "revenue": "Revenue", "guidance": "Guidance"}


def _dispute_line_en(quality: "EarningsQuality") -> str:
    """What the reader has to know when a scored leg rests on two figures
    that disagree.

    Since 2026-08-03(b) such a leg CAN be a beat, so the weakness has to be
    stated where the numbers are, not left to be inferred from a flag. Both
    actuals are named: the gap is usually GAAP against the street's adjusted
    basis, which is a fact about the quarter the Adversary should be able to
    attack directly.
    """
    leg = quality.eps
    if "actual_disputed" not in leg.flags:
        return ""
    figures = " vs ".join(f"{v:g}" for v in (leg.actual_yahoo,
                                             leg.actual_finnhub)
                          if v is not None)
    return (f" NOTE: the two data vendors report DIFFERENT EPS actuals for "
            f"this quarter ({figures}). The reading above takes the smaller "
            f"actual against the larger consensus, so it holds under either "
            f"vendor's numbers — but the basis is not settled, and the "
            f"company's own release has been requested to settle it.")


def _leg_line_en(leg: LegVerdict) -> str:
    head = f"{_EN_LEG.get(leg.leg, leg.leg)} {_EN_STATUS[leg.status]} consensus"
    if leg.surprise_pct is not None:
        head += f" {leg.surprise_pct:+.1f}% (conservative of the two readings"
        pair = [f"Yahoo {leg.yahoo_surprise_pct:+.1f}%"
                if leg.yahoo_surprise_pct is not None else "",
                f"Finnhub {leg.finnhub_surprise_pct:+.1f}%"
                if leg.finnhub_surprise_pct is not None else ""]
        head += ": " + ", ".join(p for p in pair if p) + ")"
    if leg.analysts is not None:
        head += f", {leg.analysts} analysts"
    if leg.flags:
        head += f" [{', '.join(leg.flags)}]"
    return head


def describe_quality_en(quality: "EarningsQuality") -> str:
    """The catalyst text the tribunal reads.

    English, unlike the reports: the Bull and the Adversary argue in English,
    and a mixed-language brief is one more thing for them to misread. The
    unverified rule is spelled out rather than implied, because the prompts
    tell both roles to prefer structured numbers over prose — so a leg that
    is NOT a fact has to say so in the same place they are looking.
    """
    legs = ", ".join(_leg_line_en(leg) for leg in quality.legs)
    tail = ("A leg marked UNVERIFIED is a known unknown: it earns no ranking "
            "score and must not be argued as a beat.")
    return (f"Earnings quality on three legs (a beat requires BOTH sources to "
            f"agree): {legs}. {tail}{_dispute_line_en(quality)}")


def print_from_event(event: EarningsEvent, stock_id: str,
                     fiscal_quarter: Optional[str] = None) -> EarningsPrint:
    """One calendar event as a print row, with each actual attributed.

    Which vendor supplied which number is the whole point of the row: after
    verification the event carries Yahoo's reading in `eps_actual` and the
    calendar's in `calendar_eps_actual`, and collapsing them back into one
    field would destroy the comparison the beat rule depends on.

    The fiscal quarter is left EMPTY when no source stated one. It used to
    fall back to the calendar quarter of the report date, which named the
    following quarter for any company whose period does not end in the month
    it reports — and under a wrong label the row neither finds its
    pre-registered consensus nor collides with the correct row, so the error
    left no trace anywhere (EW移行 §2).
    """
    verified = event.eps_source == "yahoo"
    yahoo_actual = event.eps_actual if verified else None
    finnhub_actual = (event.calendar_eps_actual if verified
                      else event.eps_actual)
    # Every actual the calendar gave for this print, not just the row that
    # won the collapse: two of them means the source contradicts itself.
    finnhub_actuals = list(event.all_eps_actuals) or (
        [finnhub_actual] if finnhub_actual is not None else [])
    return EarningsPrint(
        stock_id=stock_id, ticker=event.ticker,
        fiscal_quarter=(fiscal_quarter or event.fiscal_quarter or ""),
        report_date=event.day,
        depth=PrintDepth.VERIFIED if verified else PrintDepth.CALENDAR_ONLY,
        eps_yahoo=yahoo_actual,
        eps_finnhub=finnhub_actuals,
        revenue_finnhub=event.revenue_actual)


def reconstructed_consensus(event: EarningsEvent, stock_id: str,
                            fiscal_quarter: Optional[str] = None,
                            captured_at=None) -> ConsensusSnapshot:
    """The consensus as it can be read AFTER the print — weaker evidence.

    Marked `reconstructed` so it is never mistaken for a pre-registered row.
    It carries no analyst count and no range, because those only exist in the
    forward-looking endpoint, i.e. only before the release. Recomputing a
    surprise from these estimates also understates a beat slightly: Yahoo
    displays a rounded estimate while publishing a surprise computed from
    full precision. Understating is the safe direction here, and it is the
    same conservative rule applied everywhere else.

    Its fiscal quarter is left empty when no source stated one, for the same
    reason as `print_from_event`.
    """
    verified = event.eps_source == "yahoo"
    return ConsensusSnapshot(
        stock_id=stock_id, ticker=event.ticker,
        fiscal_quarter=(fiscal_quarter or event.fiscal_quarter or ""),
        captured_at=captured_at or now(),
        kind=SnapshotKind.RECONSTRUCTED,
        expected_report_date=event.day,
        eps_avg=event.eps_estimate if verified else None,
        eps_finnhub=(event.calendar_eps_estimate if verified
                     else event.eps_estimate),
        revenue_finnhub=event.revenue_estimate,
        source_note="reconstructed after the print; no analyst count or range")


def assess_event(event: EarningsEvent,
                 consensus: Optional[ConsensusSnapshot],
                 config,
                 gap_on_event_pct: Optional[float] = None,
                 stock_id: str = "") -> EarningsQuality:
    """Judge a calendar event, falling back to a reconstructed consensus.

    A reconstruction still gives two opinions whenever verification ran, so
    the "both sources agree" rule is checkable even before any
    pre-registration exists — that is what lets the funnel work from day one
    while the pre-registered rows accumulate.
    """
    row = print_from_event(event, stock_id)
    if consensus is None:
        consensus = reconstructed_consensus(event, stock_id,
                                            row.fiscal_quarter)
    return assess_earnings(row, consensus, config, gap_on_event_pct)


def assess_earnings(print_row: EarningsPrint,
                    consensus: Optional[ConsensusSnapshot],
                    config,
                    gap_on_event_pct: Optional[float] = None
                    ) -> EarningsQuality:
    """Judge one quarter against the consensus that was in force before it.

    `gap_on_event_pct` keeps the existing ranking term: a modest positive
    reaction confirms the print without exhausting it, and a negative one is
    the market disagreeing with the number we just validated.
    """
    eps_actual, eps_flags = _resolve_actual(
        print_row.eps_yahoo, print_row.eps_finnhub_usable,
        print_row.eps_release, config,
        absolute_floor=config.earnings_actual_dispute_abs_usd)
    if print_row.eps_finnhub and print_row.eps_finnhub_usable is None:
        eps_flags.insert(0, "finnhub_actual_conflict")
    eps = _assess_leg(
        "eps", eps_actual, eps_flags,
        consensus.eps_avg if consensus else None,
        consensus.eps_finnhub if consensus else None,
        consensus.eps_analysts if consensus else None, config,
        min_abs_estimate=config.scout_min_abs_eps_estimate,
        basis=print_row.eps_basis,
        actual_yahoo=print_row.eps_yahoo,
        actual_finnhub=print_row.eps_finnhub_usable)

    revenue_actual, revenue_flags = _resolve_actual(
        print_row.revenue_xbrl or print_row.revenue_release,
        print_row.revenue_finnhub, None, config, absolute_floor=None)
    revenue = _assess_leg(
        "revenue", revenue_actual, revenue_flags,
        consensus.revenue_avg if consensus else None,
        consensus.revenue_finnhub if consensus else None,
        consensus.revenue_analysts if consensus else None, config)

    guidance = _guidance_leg(print_row, consensus)

    score = score_candidate(eps.scored_pct, revenue.scored_pct,
                            gap_on_event_pct)
    # A leg that MISSED subtracts. Only a miss does: unverified and absent
    # legs score zero, because "we could not check" must never read as "it
    # went badly" any more than it reads as "it went well" (invariant 6).
    if eps.status is LegStatus.MISS:
        score += eps_points(eps.surprise_pct)
    if revenue.status is LegStatus.MISS:
        score += revenue_points(revenue.surprise_pct)
    if guidance.status is LegStatus.BEAT:
        score += config.guidance_beat_score
    score = round(score, 2)

    flags = list(dict.fromkeys(
        list(eps.flags) + [f"revenue_{f}" for f in revenue.flags]))
    if print_row.eps_basis is EpsBasis.UNADJUSTED and eps_actual is not None:
        flags.append("unadjusted")
    return EarningsQuality(
        ticker=print_row.ticker, fiscal_quarter=print_row.fiscal_quarter,
        eps=eps, revenue=revenue, guidance=guidance,
        verdict=_verdict(eps, revenue, guidance), score=score,
        flags=tuple(flags))
