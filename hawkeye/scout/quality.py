"""Judging one quarter on three legs: EPS, revenue, guidance (§5.3).

The question this module answers is "was this a good quarter", and the
answer it is allowed to give is deliberately narrow.

**Every percentage here is one vendor's actual over that SAME vendor's
consensus.** Which vendor is decided once per print, before the ranking
(`hawkeye/scout/numbers.py`), and recorded on the print row. This replaced a
rule that compared two vendors and scored the conservative of the two
readings — which sounded safe and was not: the figures it chose between were
an adjusted-basis consensus and a possibly-GAAP actual, so "conservative" meant
picking the smaller of two numbers that were never measuring the same thing
(EW移行 Ver2 §1).

What the other vendor said is still recorded and still reaches the reader —
the print row keeps the calendar's actuals, the consensus row keeps its point
estimate, and a material gap between the two actuals becomes a flag the
Adversary can attack. None of it enters the arithmetic.

Two failure modes remain, and they are different in kind:

- **A vendor contradicting ITSELF** (AMZN's calendar rows: 1.88 and 1.97 for
  one print). Its actual is unusable for that print — picking the row that
  happens to look right is exactly the judgment this system exists to remove.
- **The consensus is too thin or too small** (INVH's single analyst; a REIT's
  near-zero GAAP consensus). Not a disagreement at all; the denominator just
  cannot carry a percentage.

Nothing here computes a non-GAAP figure, and nothing reads one either: with
no source for a published adjusted figure, a quarter whose one-off nobody
quantified simply travels onward on the chosen vendor's numbers, with the
other vendor's disagreement stated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from hawkeye.contracts.models import now
from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    EarningsPrint,
    PrintSource,
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

    `actual` and `estimate` come from the SAME vendor, named in `source`.
    There is no conservative-of-two reading any more, because there is no
    second pair to be conservative between: a ratio built from one vendor's
    actual over another's consensus compares an adjusted-basis estimate with a
    possibly GAAP figure, and the resulting percentage means nothing however
    conservatively it is chosen (EW移行 Ver2 §1).

    What the OTHER vendor said is not thrown away — it is kept on the print
    row (`eps_actual_rows`) and the consensus row (`eps_calendar`), and when
    the two actuals differ materially the leg says so in a flag. It just no
    longer decides anything.
    """
    leg: str
    status: LegStatus
    surprise_pct: Optional[float] = None
    actual: Optional[float] = None
    estimate: Optional[float] = None
    source: str = ""                       # the vendor BOTH figures came from
    # What the other vendor reported for the same leg, for the record and for
    # the Adversary to attack. Never an input to the reading above.
    other_actual: Optional[float] = None
    analysts: Optional[int] = None
    # Machine-readable reasons, never prose. The Japanese wording lives in
    # `hawkeye/reports/quality_ja.py` and the English wording in
    # `describe_quality_en` — a verdict that carried its own sentence would
    # decide the reader's language here, in the wrong layer.
    flags: tuple[str, ...] = ()
    # Every comparison this verdict rests on, as (unit, surprise %). Only the
    # guidance leg uses more than one: a company can guide EPS and sales at
    # once, and reading only the first throws away half of what it published.
    # `surprise_pct` above stays the primary reading so the existing display
    # and the existing scoring of the other two legs are unchanged.
    parts: tuple[tuple[str, float], ...] = ()

    @property
    def scored_pct(self) -> Optional[float]:
        """What ranking may use: a confirmed beat, or nothing."""
        return self.surprise_pct if self.status is LegStatus.BEAT else None

    @property
    def beat_fraction(self) -> float:
        """The share of the compared legs that came in above consensus.

        A company guiding two legs and beating on one has said something
        weaker than one beating on both, and something stronger than one
        beating on neither. The bonus follows that share rather than an
        all-or-nothing reading of the first leg that happened to be present.
        """
        if not self.parts:
            return 0.0
        return sum(1 for _, pct in self.parts if pct > 0) / len(self.parts)


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


def _assess_leg(leg: str, actual: Optional[float], actual_flags: list[str],
                estimate: Optional[float], analysts: Optional[int], config,
                source: str = "",
                min_abs_estimate: Optional[float] = None,
                other_actual: Optional[float] = None) -> LegVerdict:
    """One leg read from ONE vendor's actual over the SAME vendor's consensus.

    This used to take two estimates and score the conservative of the two
    readings. That rule died with the move to one vendor per print: the
    figures it compared were an adjusted-basis consensus and a possibly-GAAP
    actual, so "conservative" was picking the smaller of two numbers that were
    never measuring the same thing (EW移行 Ver2 §1).

    What the other vendor reported is still recorded and, when it differs
    materially, flagged — as a fact for the Adversary to use, never as an
    input to the reading.
    """
    flags = list(actual_flags)
    pct = _pct(actual, estimate)

    if estimate is None:
        flags.append("no_consensus")
    if analysts is not None and analysts < config.earnings_min_analysts:
        flags.append("thin_coverage")
    if (min_abs_estimate is not None and estimate is not None
            and abs(estimate) < min_abs_estimate):
        flags.append("estimate_too_small")
    if (other_actual is not None and actual is not None
            and _relative_gap(actual, other_actual)
            > config.earnings_actual_dispute_pct
            and abs(actual - other_actual)
            > config.earnings_actual_dispute_abs_usd):
        flags.append("vendors_report_different_actuals")

    return LegVerdict(leg=leg, status=_leg_status(pct, estimate, flags),
                      surprise_pct=pct, actual=actual, estimate=estimate,
                      source=source, other_actual=other_actual,
                      analysts=analysts, flags=tuple(flags))


# What makes a leg unverified. `no_actual` and `no_consensus` are invariant 6
# in code: missing data scores zero and says so, and must never read either as
# a beat or as the company having done badly. The other two are doctrine
# numbers and live in config.
_BLOCKING_FLAGS = ("no_actual", "no_consensus", "thin_coverage",
                   "estimate_too_small")


def _leg_status(pct: Optional[float], estimate: Optional[float],
                flags: list[str]) -> LegStatus:
    """Unverified beats every other reading.

    A leg that cannot be confirmed must not be scored as a beat OR written
    off as a miss: both would be claims the data does not support. It scores
    zero and travels onward saying so (invariant 6).
    """
    if pct is None and estimate is None and not flags:
        return LegStatus.ABSENT
    if pct is None or any(flag in _BLOCKING_FLAGS for flag in flags):
        return LegStatus.UNVERIFIED
    if pct > 0:
        return LegStatus.BEAT
    if pct < 0:
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


def _yardsticks(period: str, print_row: EarningsPrint,
                consensus: Optional[ConsensusSnapshot]
                ) -> tuple[Optional[float], Optional[float], str]:
    """(EPS bar, revenue bar, why there is none) for the period guided.

    Two yardsticks are captured, and which one applies is decided by the
    period the company guided FOR — never by which one happens to be
    populated. Left unchecked this is not a small error: ADM guided FY2026 EPS
    of $5.15-$5.60 against a quarterly consensus near $1.20, and the
    comparison came out as a +348% guidance beat the company never gave.

    An unlabelled reading keeps its old meaning (next quarter) so that
    readings recorded before the label existed still count.
    """
    if period.startswith("FY"):
        # The full-year bar has to state its own year. A figure for another
        # year is the same cross-period error one period up, and an unstated
        # one cannot be checked at all — both refuse (invariant 6).
        stated = consensus.full_year_period if consensus else ""
        if not stated:
            return None, None, "no_full_year_consensus_to_compare"
        if stated != period:
            return None, None, "full_year_consensus_is_another_year"
        return consensus.full_year_eps_avg, consensus.full_year_revenue_avg, ""
    if period and period != _next_quarter(print_row.fiscal_quarter):
        return None, None, "guidance_period_not_comparable"
    if consensus is None:
        return None, None, ""
    return consensus.next_quarter_eps_avg, consensus.next_quarter_revenue_avg, ""


def _guidance_leg(print_row: EarningsPrint,
                  consensus: Optional[ConsensusSnapshot]) -> LegVerdict:
    """Guidance against the consensus for the SAME period, captured with it.

    Absence is neutral by design and is the normal case: there is no
    structured source for guidance on any free tier, so penalising its
    absence would quietly penalise the data gap rather than the company.
    """
    guidance = print_row.guidance
    if guidance is None:
        return LegVerdict(leg="guidance", status=LegStatus.ABSENT,
                          flags=("guidance_not_published",))
    eps_bar, revenue_bar, refusal = _yardsticks(guidance.period, print_row,
                                                consensus)
    if refusal:
        return LegVerdict(leg="guidance", status=LegStatus.ABSENT,
                          flags=(refusal, f"guided_{guidance.period}"))
    # EPS first, revenue second. Plenty of companies guide only on sales —
    # Amazon gives net sales and operating income and never an EPS range —
    # and scoring those as "no guidance" would describe our reading rather
    # than the company.
    pairs = ((guidance.eps_midpoint, eps_bar, "eps"),
             (guidance.revenue_midpoint, revenue_bar, "revenue"))
    usable = [(value, yardstick, unit) for value, yardstick, unit in pairs
              if value is not None and yardstick is not None]
    if not usable:
        return LegVerdict(leg="guidance", status=LegStatus.ABSENT,
                          flags=("no_forward_consensus_to_compare",))
    # EVERY leg the company guided and this system holds a bar for, not just
    # the first. A company that guided EPS and sales made two statements, and
    # scoring one of them describes our reading rather than the company.
    parts = tuple((unit, _pct(value, yardstick))
                  for value, yardstick, unit in usable
                  if _pct(value, yardstick) is not None)
    if not parts:
        return LegVerdict(leg="guidance", status=LegStatus.ABSENT,
                          flags=("no_forward_consensus_to_compare",))
    above = sum(1 for _, pct in parts if pct > 0)
    below = sum(1 for _, pct in parts if pct < 0)
    # Mixed reads as INLINE rather than as the stronger of the two: one leg
    # up and one down is not a beat, and it is not a miss either.
    status = (LegStatus.BEAT if above and not below
              else LegStatus.MISS if below and not above
              else LegStatus.INLINE)
    midpoint, yardstick, _ = usable[0]
    # The period is carried on the verdict, not just used to pick the bar: a
    # +13% guidance beat means a different thing for a year than for a
    # quarter, and the reader is entitled to know which one was measured.
    period_flag = (f"against_{guidance.period}",) if guidance.period else ()
    return LegVerdict(leg="guidance", status=status, surprise_pct=parts[0][1],
                      actual=midpoint, estimate=yardstick, parts=parts,
                      flags=tuple(f"on_{unit}" for unit, _ in parts)
                      + period_flag)


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
    """What the reader has to know when the two vendors report different
    actuals for the same quarter.

    The reading is not affected — it stands on one vendor's actual over the
    same vendor's consensus — but the gap is usually GAAP against the
    street's adjusted basis, which is a fact about the quarter the Adversary
    should be able to attack directly.
    """
    leg = quality.eps
    if "vendors_report_different_actuals" not in leg.flags:
        return ""
    return (f" NOTE: the earnings calendar reports a DIFFERENT EPS actual for "
            f"this quarter ({leg.other_actual:g}) than the {leg.source} figure "
            f"the reading above uses ({leg.actual:g}). Both figures stand; the "
            f"gap is usually GAAP against an adjusted basis, and nothing in "
            f"this system will settle which one the consensus was set on.")


def _leg_line_en(leg: LegVerdict) -> str:
    head = f"{_EN_LEG.get(leg.leg, leg.leg)} {_EN_STATUS[leg.status]} consensus"
    if leg.surprise_pct is not None:
        head += f" {leg.surprise_pct:+.1f}%"
        if leg.source:
            head += f" (actual and consensus both from {leg.source})"
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
    return (f"Earnings quality on three legs (each percentage is one vendor's "
            f"actual over that same vendor's consensus, never a mix): "
            f"{legs}. {tail}{_dispute_line_en(quality)}")


def print_from_event(event: EarningsEvent, stock_id: str,
                     fiscal_quarter: Optional[str] = None) -> EarningsPrint:
    """One calendar event as a print row, with each actual attributed.

    Which vendor supplied which number is the whole point of the row. The
    verified reading goes in `eps_actual` and the calendar's own rows in
    `eps_actual_rows`; `source` names the vendor behind the first. Collapsing
    them into one field would destroy the comparison the beat rule depends on.

    The fiscal quarter is left EMPTY when no source stated one. It used to
    fall back to the calendar quarter of the report date, which named the
    following quarter for any company whose period does not end in the month
    it reports — and under a wrong label the row neither finds its
    pre-registered consensus nor collides with the correct row, so the error
    left no trace anywhere (EW移行 §2).
    """
    from_feed = event.numbers_source == "whispers"
    feed_actual = event.eps_actual if from_feed else None
    calendar_actual = (event.calendar_eps_actual if from_feed
                       else event.eps_actual)
    # Every actual the calendar gave for this print, not just the row that
    # won the collapse: two of them means the source contradicts itself.
    calendar_actuals = list(event.all_eps_actuals) or (
        [calendar_actual] if calendar_actual is not None else [])
    return EarningsPrint(
        stock_id=stock_id, ticker=event.ticker,
        fiscal_quarter=(fiscal_quarter or event.fiscal_quarter or ""),
        report_date=event.day,
        reported_at=event.announced_at,
        source=PrintSource.WHISPERS if from_feed else PrintSource.FINNHUB,
        eps_actual=feed_actual,
        eps_actual_rows=calendar_actuals,
        revenue_actual=event.revenue_actual,
        guidance=event.guidance)


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
    from_feed = event.numbers_source == "whispers"
    return ConsensusSnapshot(
        stock_id=stock_id, ticker=event.ticker,
        fiscal_quarter=(fiscal_quarter or event.fiscal_quarter or ""),
        captured_at=captured_at or now(),
        kind=SnapshotKind.RECONSTRUCTED,
        expected_report_date=event.day,
        eps_avg=event.eps_estimate if from_feed else None,
        eps_calendar=(event.calendar_eps_estimate if from_feed
                      else event.eps_estimate),
        # Revenue moves with EPS or not at all: the vendor that supplied the
        # actual supplied the consensus it is measured against, so putting the
        # feed's revenue estimate in the calendar's field would recreate the
        # cross-vendor ratio one field lower down.
        revenue_avg=event.revenue_estimate if from_feed else None,
        revenue_calendar=(event.calendar_revenue_estimate if from_feed
                          else event.revenue_estimate),
        # The full-year yardstick, read off the same summary that carried the
        # guidance it measures. It is NOT governed by the one-vendor rule —
        # that rule binds the surprise ratio's own numerator and denominator —
        # so it travels even when the ratio fell back to the calendar.
        full_year_eps_avg=event.full_year_eps_estimate,
        full_year_revenue_avg=event.full_year_revenue_estimate,
        full_year_period=event.full_year_period,
        next_quarter_eps_avg=event.next_quarter_eps_estimate,
        next_quarter_revenue_avg=event.next_quarter_revenue_estimate,
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
    # One vendor decides both legs of this print, and `source` on the row is
    # who that is. The other vendor's figures are still on the row and still
    # reach the reader — they simply do not enter the arithmetic.
    from_feed = print_row.source is PrintSource.WHISPERS
    calendar_actual = print_row.eps_actual_rows_usable
    eps_actual = print_row.eps_actual if from_feed else calendar_actual
    eps_flags: list[str] = []
    if print_row.eps_actual_rows and calendar_actual is None:
        # The calendar contradicting ITSELF (AMZN: 1.88 on one row, 1.97 on
        # another). Fatal only when the calendar is the chosen vendor; when
        # the feed supplied the figures it is a fact worth recording, and the
        # reading stands on numbers the calendar never touched.
        eps_flags.append("finnhub_actual_conflict")
    if eps_actual is None:
        eps_flags.append("no_actual")
    eps = _assess_leg(
        "eps", eps_actual, eps_flags,
        (consensus.eps_avg if from_feed else consensus.eps_calendar)
        if consensus else None,
        consensus.eps_analysts if consensus else None, config,
        source=print_row.source.value,
        min_abs_estimate=config.scout_min_abs_eps_estimate,
        other_actual=calendar_actual if from_feed else None)

    revenue_actual = print_row.revenue_actual
    revenue = _assess_leg(
        "revenue", revenue_actual,
        [] if revenue_actual is not None else ["no_actual"],
        (consensus.revenue_avg if from_feed else consensus.revenue_calendar)
        if consensus else None,
        consensus.revenue_analysts if consensus else None, config,
        source=print_row.source.value)

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
    # The share of the guided legs that beat, so a company guiding both EPS
    # and sales and beating on one earns half of what beating on both earns.
    # A leg that came in BELOW subtracts nothing, same as before: "no
    # guidance" and "weak guidance" must not collapse into one number
    # (§5.3 決定3).
    score += config.guidance_beat_score * guidance.beat_fraction
    score = round(score, 2)

    flags = list(dict.fromkeys(
        list(eps.flags) + [f"revenue_{f}" for f in revenue.flags]))
    return EarningsQuality(
        ticker=print_row.ticker, fiscal_quarter=print_row.fiscal_quarter,
        eps=eps, revenue=revenue, guidance=guidance,
        verdict=_verdict(eps, revenue, guidance), score=score,
        flags=tuple(flags))
