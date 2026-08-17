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

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional

from hawkeye.contracts.models import ScoreBreakdown, now
from hawkeye.contracts.stocks import (
    CauseReading,
    ConsensusSnapshot,
    EarningsPrint,
    PrintSource,
    SnapshotKind,
    next_fiscal_quarter,
)
from hawkeye.scout.cause_agent import failure_kind
from hawkeye.scout.earnings import (
    EarningsEvent,
    eps_points,
    revenue_points,
    score_parts,
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
    # Source text this verdict rests on or refuses on, quoted verbatim. NOT a
    # sentence of ours — `flags` carries our reasoning and stays machine
    # readable. Today only the guidance leg fills it, with the condition the
    # company attached to a range (§5.3, layer 3): a refusal the reader cannot
    # see the words behind is a refusal they cannot check.
    excerpt: str = ""

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

    @property
    def miss_fraction(self) -> float:
        """The share of the compared legs that came in BELOW consensus.

        The mirror of `beat_fraction`, and it has to be a separate reading
        rather than `1 - beat_fraction`: a leg that landed exactly on
        consensus is neither, and folding it into the miss would charge a
        company for meeting the number.

        Zero when nothing was compared, which is what keeps absence free —
        no outlook, an outlook we declined to compare, and no yardstick all
        produce empty `parts` (hawkeye/config.py `guidance_miss_penalty`).
        """
        if not self.parts:
            return 0.0
        return sum(1 for _, pct in self.parts if pct < 0) / len(self.parts)


@dataclass(frozen=True)
class EarningsQuality:
    ticker: str
    fiscal_quarter: str
    eps: LegVerdict
    revenue: LegVerdict
    guidance: LegVerdict
    verdict: QuarterVerdict
    score: float
    # What earned `score`, part by part. Carried on the verdict because the
    # scan report shows the ranking to the user and asks them to accept it,
    # and a ranking term the reader cannot see is a ranking term they cannot
    # argue with — the same reason `whisper_beat_pct` sits here.
    breakdown: Optional[ScoreBreakdown] = None
    flags: tuple[str, ...] = field(default_factory=tuple)
    # The feed's unofficial expectation and how far the print cleared it, kept
    # on the verdict because they moved `score` above. A ranking term the
    # reader cannot see is a ranking term they cannot argue with.
    whisper: Optional[float] = None
    whisper_beat_pct: Optional[float] = None
    # WHO read the guidance sentence and with which model. Carried on the
    # verdict, not only on the stored row, because the inspection table is
    # built from verdicts and this is the one column that says whether two
    # runs' guidance readings are even comparable (EW移行 Ver2 §13.3). Empty
    # when the print carries no guidance reading at all.
    guidance_extractor: str = ""
    guidance_extractor_model: str = ""
    # The company's own account of this quarter, and — when there is none —
    # the NAMED reason there is none. Both travel on the verdict because both
    # have to reach the tribunal: a missing explanation that arrives as
    # silence reads as "there was nothing to explain", which is the specific
    # false impression this feature exists to remove (T-003).
    cause: Optional[CauseReading] = None
    cause_reason: str = ""

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
# a beat or as the company having done badly. `estimate_too_small` is a
# doctrine number and lives in config.
_BLOCKING_FLAGS = ("no_actual", "no_consensus", "estimate_too_small")


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


_next_quarter = next_fiscal_quarter    # the shared label arithmetic


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
                  consensus: Optional[ConsensusSnapshot],
                  config) -> LegVerdict:
    """Guidance against the consensus for the SAME period, captured with it.

    Absence is neutral by design and is the normal case: there is no
    structured source for guidance on any free tier, so penalising its
    absence would quietly penalise the data gap rather than the company.
    """
    guidance = print_row.guidance
    if guidance is None:
        # WHY there is none, when the row knows. "The company published no
        # outlook", "the reader could not read the one it published" and "the
        # extraction call failed" are three different facts, and a leg that
        # renders all three as 開示なし makes the second and third invisible
        # exactly where they would be noticed.
        return LegVerdict(
            leg="guidance", status=LegStatus.ABSENT,
            flags=(print_row.guidance_reason or "guidance_not_published",))
    # A range the company fenced with a condition is not measured against a
    # consensus set without one. ACA guided "2026 revenue of $2.60 to $2.70
    # billion, EXCLUDING its barge business" while the analysts' figure for
    # the same year was "$3.02 billion, WHICH INCLUDES its barge business" —
    # the two describe different companies, and the -12% that falls out of
    # dividing them is a miss ACA never guided.
    #
    # Fails CLOSED because the alternative needs a judgment nothing here can
    # make: sizing the excluded business is exactly the estimate this system
    # exists to avoid inventing. The condition is quoted so the reader can see
    # what was declined, and it costs nothing either way (see below: an absent
    # guidance leg is neither scored nor penalised).
    if guidance.qualifier:
        return LegVerdict(leg="guidance", status=LegStatus.ABSENT,
                          flags=("guidance_scope_qualified",),
                          excerpt=guidance.qualifier)
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
    # The EPS yardstick can sit as close to zero as a EPS consensus can — ALGT
    # guided "a loss of $1.00 per share to breakeven" against a $0.08
    # consensus, i.e. -725% — so it reuses the EPS leg's own floor rather than
    # a second doctrine number for the same kind of figure (Set H-1,
    # docs/design/SET_H_G_DECISIONS.ja.md). Revenue yardsticks are not
    # floored, matching the revenue leg above.
    too_small = any(unit == "eps" and abs(yardstick) < config.scout_min_abs_eps_estimate
                    for _, yardstick, unit in usable)
    usable = [(value, yardstick, unit) for value, yardstick, unit in usable
              if unit != "eps"
              or abs(yardstick) >= config.scout_min_abs_eps_estimate]
    if not usable:
        return LegVerdict(
            leg="guidance", status=LegStatus.ABSENT,
            flags=(("eps_yardstick_too_small",) if too_small
                  else ("no_forward_consensus_to_compare",)))
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
    too_small_flag = ("eps_yardstick_too_small",) if too_small else ()
    return LegVerdict(leg="guidance", status=status, surprise_pct=parts[0][1],
                      actual=midpoint, estimate=yardstick, parts=parts,
                      flags=tuple(f"on_{unit}" for unit, _ in parts)
                      + period_flag + too_small_flag)


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


_CAUSE_ABSENCE_EN = {
    "absent_in_source": "the source states none",
    "source_absent": "the vendor supplied no summary to read",
    "reader_failed": "the source states one and our reader could not accept it",
    "call_failed": "the extraction call did not complete",
    "not_yet_read": "it has not been read yet",
}

_CAUSE_NATURE_EN = {
    "one_off": ("an item the company itself describes as not repeating — a "
                "tax item, a gain or charge, a settlement or a revaluation"),
    "operating": ("the business itself — pricing, cost, mix or volume — by "
                  "the company's own description"),
    "unclear": ("something the company states without saying whether it "
                "repeats"),
}


def _cause_line_en(quality: "EarningsQuality") -> str:
    """What the company said explains the quarter — or that nothing does.

    Both halves have to be said out loud. A headline EPS beat beside flat
    revenue is the shape of almost every candidate that reaches the tribunal,
    and it has two opposite meanings; with nothing written here the roles
    filled the gap themselves and wrote the guess down as established fact
    ("explicable by tax effects or revaluation gains" — PGY). Saying "we did
    not read one" is what makes that guess visibly a guess.
    """
    cause = quality.cause
    if cause is None:
        why = _CAUSE_ABSENCE_EN.get(
            failure_kind(quality.cause_reason) if quality.cause_reason
            else "not_yet_read", "it has not been read yet")
        return (f" NOTE: no explanation of this quarter was read from the "
                f"source ({why}). The reason for any gap between the EPS and "
                f"the revenue line above is therefore UNVERIFIED: do not "
                f"argue a one-off, a tax effect or a margin improvement that "
                f"nothing here recorded.")
    size = ""
    if cause.magnitude is not None and cause.magnitude_unit:
        size = f" It sizes it at {cause.magnitude:g} ({cause.magnitude_unit})."
    return (f' NOTE: the company\'s own explanation of this quarter, carried '
            f'on the EPS and revenue legs above, reads: "{cause.source_excerpt}"'
            f" — which is "
            f"{_CAUSE_NATURE_EN.get(cause.nature, _CAUSE_NATURE_EN['unclear'])}."
            f"{size} This is what the company SAID, verified only to exist in "
            f"the source word for word — nothing here checked whether it is "
            f"true or whether it accounts for the whole surprise.")


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
    # The source's own words behind a refusal. The Adversary can only attack
    # "guided above consensus on terms nobody reconciled" if it is told what
    # those terms were.
    #
    # Guidance only. The EPS and revenue legs carry an excerpt too — the
    # company's account of the quarter, which is about both of them at once
    # (T-003) — and printing it on each would put one long sentence into this
    # paragraph three times over. It is stated once, in the NOTE below, where
    # what it is and what it is worth can be said in the same breath. A reader
    # holding a single `LegVerdict` still finds it on the leg.
    if leg.excerpt and leg.leg == "guidance":
        head += f' — the company\'s own condition: "{leg.excerpt}"'
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
            f"{legs}. {tail}{_dispute_line_en(quality)}"
            f"{_cause_line_en(quality)}")


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
        # Bound by the same one-vendor rule as the pair above: a whisper is an
        # expectation the FEED published, and it only means something beside an
        # actual the feed also supplied.
        eps_whisper=event.whisper if from_feed else None,
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


def whisper_beat_pct(print_row: EarningsPrint,
                     consensus: Optional[ConsensusSnapshot],
                     eps: LegVerdict) -> Optional[float]:
    """How far the reported EPS cleared the feed's unofficial expectation, or
    None when the two cannot be compared.

    Three conditions, and all three are refusals rather than fallbacks:

    - the feed supplied the actual. A calendar actual over a feed whisper is
      two vendors in one ratio, which is what task 7.5 removed everywhere else.
    - the EPS leg is a confirmed beat. An unverified leg earns nothing anywhere
      (invariant 6), and a leg that missed consensus has already been scored.
    - the whisper is not zero, which would make the percentage meaningless.
    """
    if print_row.source is not PrintSource.WHISPERS:
        return None
    if eps.scored_pct is None or eps.actual is None:
        return None
    whisper = consensus.eps_whisper if consensus else None
    if whisper is None or whisper == 0:
        return None
    return (eps.actual - whisper) / abs(whisper) * 100.0


def whisper_points(beat_pct: Optional[float], config) -> float:
    """Ranking points for clearing the whisper. Never negative: see
    `whisper_beat_weight` in `hawkeye/config.py` for why the signal adds but
    does not subtract."""
    if beat_pct is None or beat_pct <= 0:
        return 0.0
    return min(beat_pct * config.whisper_beat_weight, config.whisper_beat_cap)


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

    # The company's own account of THIS quarter, on both legs it speaks to.
    # It sits on the legs rather than beside them because the question it
    # answers — is a headline beat next to flat revenue a margin the company
    # earned, or an item that will not repeat — is a question about those two
    # numbers, and the roles read the legs (T-003). It changes no status and
    # no score: it is what the company said, not a second opinion on whether
    # the print was good.
    if print_row.cause is not None and print_row.cause.source_excerpt:
        eps = replace(eps, excerpt=print_row.cause.source_excerpt)
        revenue = replace(revenue, excerpt=print_row.cause.source_excerpt)

    guidance = _guidance_leg(print_row, consensus, config)

    eps_part, revenue_part, gap_part = score_parts(
        eps.scored_pct, revenue.scored_pct, gap_on_event_pct)
    score = round(eps_part + revenue_part + gap_part, 2)
    # A leg that MISSED subtracts. Only a miss does: unverified and absent
    # legs score zero, because "we could not check" must never read as "it
    # went badly" any more than it reads as "it went well" (invariant 6).
    if eps.status is LegStatus.MISS:
        eps_part += eps_points(eps.surprise_pct)
        score += eps_points(eps.surprise_pct)
    if revenue.status is LegStatus.MISS:
        revenue_part += revenue_points(revenue.surprise_pct)
        score += revenue_points(revenue.surprise_pct)
    # The share of the guided legs that beat, so a company guiding both EPS
    # and sales and beating on one earns half of what beating on both earns.
    # A leg that came in BELOW now subtracts on the same terms, reversing
    # §5.3 決定3 (User decision 2026-08-11): leaving a published shortfall at
    # zero made it score identically to having published nothing at all.
    # Absence still costs nothing — see `guidance_miss_penalty` in
    # hawkeye/config.py for which three cases that covers and why.
    guidance_part = (config.guidance_beat_score * guidance.beat_fraction
                     - config.guidance_miss_penalty * guidance.miss_fraction)
    score += guidance_part
    whisper_beat = whisper_beat_pct(print_row, consensus, eps)
    whisper_part = whisper_points(whisper_beat, config)
    score += whisper_part
    score = round(score, 2)
    # Rounded to the same two places the score is, so the five parts a reader
    # adds up on screen come to the total printed beside them.
    breakdown = ScoreBreakdown(
        eps=round(eps_part, 2), revenue=round(revenue_part, 2),
        gap=round(gap_part, 2), guidance=round(guidance_part, 2),
        whisper=round(whisper_part, 2))

    flags = list(dict.fromkeys(
        list(eps.flags) + [f"revenue_{f}" for f in revenue.flags]))
    return EarningsQuality(
        ticker=print_row.ticker, fiscal_quarter=print_row.fiscal_quarter,
        eps=eps, revenue=revenue, guidance=guidance,
        verdict=_verdict(eps, revenue, guidance), score=score,
        breakdown=breakdown, flags=tuple(flags),
        whisper=(consensus.eps_whisper if consensus and whisper_beat is not None
                 else None),
        whisper_beat_pct=(round(whisper_beat, 4)
                          if whisper_beat is not None else None),
        guidance_extractor=(print_row.guidance.extractor
                            if print_row.guidance else ""),
        guidance_extractor_model=(print_row.guidance.extractor_model
                                  if print_row.guidance else ""),
        cause=print_row.cause, cause_reason=print_row.cause_reason)
