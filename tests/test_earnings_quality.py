"""Judging a quarter on three legs (docs/design/MASTER_OVERVIEW.ja.md §5.3).

Every case here is a name that was actually measured, because the design came
out of those measurements rather than out of theory: AMZN's calendar returning
two different actuals for one print, AAPL's two correct-but-different actuals,
BIIB's consensus that moved 3.98 -> 2.15, INVH's consensus built from one
analyst.

The rule the tests defend, since 2026-08-07: **every percentage is one
vendor's actual over that SAME vendor's consensus.** Which vendor is chosen
once per print, before the ranking, and recorded on the row. What the other
vendor said is kept and reported and never enters the arithmetic — because a
ratio built from an adjusted-basis consensus and a possibly-GAAP actual has no
referent, however conservatively its inputs are picked.

Everything that cannot be confirmed still scores zero rather than passing
quietly.
"""
from __future__ import annotations

from datetime import date

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    EarningsPrint,
    GuidanceReading,
    PrintSource,
    SnapshotKind,
)
from hawkeye.scout.quality import LegStatus, QuarterVerdict, assess_earnings

CONFIG = HawkeyeConfig()


def a_print(**overrides) -> EarningsPrint:
    """A print the earnings feed supplied.

    `eps_actual` is the chosen vendor's figure; `eps_actual_rows` is what the
    calendar returned for the same print, kept for the record.
    """
    base = dict(stock_id="cik:0001018724", ticker="TEST",
                fiscal_quarter="2026-Q2", report_date=date(2026, 7, 31),
                source=PrintSource.WHISPERS)
    base.update(overrides)
    return EarningsPrint(**base)


def a_calendar_print(**overrides) -> EarningsPrint:
    """A print the feed could not answer for, so the calendar stands."""
    return a_print(source=PrintSource.FINNHUB, **overrides)


def a_consensus(**overrides) -> ConsensusSnapshot:
    """`*_avg` is the feed's consensus, `*_calendar` the calendar's point
    estimate. Which one a leg uses is decided by the print's `source`."""
    base = dict(stock_id="cik:0001018724", ticker="TEST",
                fiscal_quarter="2026-Q2", kind=SnapshotKind.PRE_REGISTERED,
                eps_avg=1.00, eps_calendar=1.00, eps_analysts=20,
                revenue_avg=1.0e9, revenue_calendar=1.0e9, revenue_analysts=18)
    base.update(overrides)
    return ConsensusSnapshot(**base)


# --- one vendor decides the whole ratio -----------------------------------

def test_a_feed_backed_beat_is_measured_on_the_feeds_own_pair():
    quality = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20]),
        a_consensus(eps_avg=1.00, eps_calendar=1.00), CONFIG)

    assert quality.eps.status is LegStatus.BEAT
    assert quality.eps.surprise_pct == 20.0
    assert quality.eps.source == "whispers"
    assert quality.eps.actual == 1.20 and quality.eps.estimate == 1.00


def test_a_calendar_backed_print_is_measured_on_the_calendars_own_pair():
    """The feed declining sends the WHOLE print to the calendar, so the leg
    reads the calendar's actual against the calendar's estimate."""
    quality = assess_earnings(
        a_calendar_print(eps_actual_rows=[1.20]),
        a_consensus(eps_avg=None, eps_calendar=1.00), CONFIG)

    assert quality.eps.status is LegStatus.BEAT
    assert quality.eps.source == "finnhub"
    assert quality.eps.actual == 1.20 and quality.eps.estimate == 1.00


def test_the_other_vendors_consensus_cannot_veto_a_beat():
    """BIIB: 3.98 from one vendor, 2.15 from the other. The old rule scored
    the conservative of the two readings and called that safety; it was not.
    The two figures are on different accounting bases, so the smaller is not
    a stricter reading of the same thing — it is a different measurement.

    The reading now stands on the pair that belong together, and the vendor
    the print did NOT come from has no vote.
    """
    quality = assess_earnings(
        a_print(eps_actual=2.58, eps_actual_rows=[2.58]),
        a_consensus(eps_avg=2.15, eps_calendar=3.98), CONFIG)

    assert quality.eps.status is LegStatus.BEAT
    assert quality.eps.estimate == 2.15
    assert round(quality.eps.surprise_pct, 1) == 20.0


def test_a_feed_backed_print_with_no_feed_consensus_is_unverified():
    """Never quietly borrows the calendar's estimate. In production this pair
    cannot come apart — the feed only wins when it supplied both figures — so
    a row in this shape means something upstream broke, and inventing a
    denominator would hide it behind a plausible percentage.
    """
    quality = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20]),
        a_consensus(eps_avg=None, eps_analysts=None, eps_calendar=1.00),
        CONFIG)

    assert quality.eps.status is LegStatus.UNVERIFIED
    assert "no_consensus" in quality.eps.flags
    assert quality.score == 0.0


# --- what the other vendor said is recorded, not used ---------------------

def test_a_differing_actual_from_the_other_vendor_is_named_not_used():
    """AAPL: the feed 2.02 (matches the filing), the calendar 1.91 (= 2.02
    minus $0.11 of tariff refunds). Neither is wrong; the bases differ. The
    reading uses the feed's pair, and the gap becomes a fact the Adversary
    can attack."""
    quality = assess_earnings(
        a_print(eps_actual=2.02, eps_actual_rows=[1.91]),
        a_consensus(eps_avg=1.89, eps_calendar=1.89), CONFIG)

    assert quality.eps.status is LegStatus.BEAT
    assert quality.eps.actual == 2.02
    assert quality.eps.other_actual == 1.91
    assert "vendors_report_different_actuals" in quality.eps.flags
    assert round(quality.eps.surprise_pct, 2) == 6.88


def test_a_penny_of_rounding_is_not_worth_reporting():
    quality = assess_earnings(
        a_print(eps_actual=1.11, eps_actual_rows=[1.10]),
        a_consensus(eps_avg=1.00, eps_calendar=1.00), CONFIG)

    assert "vendors_report_different_actuals" not in quality.eps.flags
    assert quality.eps.status is LegStatus.BEAT
    assert quality.eps.surprise_pct == 11.0        # the feed's own actual


def test_the_tribunal_is_told_the_vendors_report_different_actuals():
    """The prompts tell the Bull and the Adversary to prefer structured
    numbers over prose, so a figure another vendor contradicts has to carry
    that in the same place they are looking."""
    from hawkeye.scout.quality import describe_quality_en

    text = describe_quality_en(assess_earnings(
        a_print(eps_actual=2.02, eps_actual_rows=[1.91]),
        a_consensus(eps_avg=1.89, eps_calendar=1.89), CONFIG))

    assert "vendors_report_different_actuals" in text
    assert "2.02" in text and "1.91" in text
    assert "whispers" in text


def test_the_calendar_contradicting_itself_only_bites_when_it_is_the_source():
    """AMZN's calendar returned 1.88 AND 1.97 for one print. When the feed
    supplied the figures this is recorded and nothing more — the reading
    never touched the calendar. When the calendar IS the source, its actual
    is unusable and the leg is unverified: picking the more plausible row is
    exactly the judgment this system exists to remove."""
    from_feed = assess_earnings(
        a_print(eps_actual=5.75, eps_actual_rows=[1.88, 1.97]),
        a_consensus(eps_avg=1.83, eps_calendar=1.83), CONFIG)
    from_calendar = assess_earnings(
        a_calendar_print(eps_actual_rows=[1.88, 1.97]),
        a_consensus(eps_avg=None, eps_calendar=1.83), CONFIG)

    assert "finnhub_actual_conflict" in from_feed.eps.flags
    assert from_feed.eps.status is LegStatus.BEAT
    assert from_feed.score <= 70.0                    # capped, never +215

    assert from_calendar.eps.status is LegStatus.UNVERIFIED
    assert "no_actual" in from_calendar.eps.flags
    assert from_calendar.score == 0.0


# --- what still makes a leg unverified ------------------------------------

def test_a_leg_with_no_actual_at_all_is_unverified():
    """Invariant 6: missing data scores zero and says so. It must never read
    as a beat, nor as the company having done badly."""
    quality = assess_earnings(
        a_print(), a_consensus(eps_avg=1.00, eps_calendar=1.00), CONFIG)

    assert quality.eps.status is LegStatus.UNVERIFIED
    assert "no_actual" in quality.eps.flags


def test_a_consensus_from_too_few_analysts_is_unverified():
    """INVH's EPS consensus was built from ONE analyst, and nothing in the
    vendor response says so — only a pre-registered row with a count does."""
    quality = assess_earnings(
        a_print(eps_actual=1.50, eps_actual_rows=[1.50]),
        a_consensus(eps_avg=1.00, eps_calendar=1.00, eps_analysts=1), CONFIG)

    assert quality.eps.status is LegStatus.UNVERIFIED
    assert "thin_coverage" in quality.eps.flags


def test_a_near_zero_consensus_cannot_buy_a_ranking_slot():
    """REITs report FFO, so their GAAP EPS consensus sits near zero and the
    ratio measures the denominator, not the beat (the existing guard)."""
    quality = assess_earnings(
        a_print(eps_actual=0.30, eps_actual_rows=[0.30]),
        a_consensus(eps_avg=0.02, eps_calendar=0.02), CONFIG)

    assert quality.eps.status is LegStatus.UNVERIFIED
    assert "estimate_too_small" in quality.eps.flags
    assert quality.score == 0.0


def test_no_consensus_row_at_all_is_unverified_not_a_beat():
    quality = assess_earnings(a_print(eps_actual=1.20), None, CONFIG)
    assert quality.eps.status is LegStatus.UNVERIFIED
    assert quality.verdict is QuarterVerdict.UNVERIFIED


# --- revenue: the same vendor, or nothing ---------------------------------

def test_revenue_is_measured_on_the_same_vendor_as_eps():
    beat = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20],
                revenue_actual=1.05e9),
        a_consensus(revenue_avg=1.0e9, revenue_calendar=1.0e9), CONFIG)
    flat = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20]),
        a_consensus(revenue_avg=None, revenue_calendar=None), CONFIG)

    assert beat.revenue.status is LegStatus.BEAT
    assert beat.revenue.source == "whispers"
    assert round(beat.revenue.surprise_pct, 1) == 5.0
    assert beat.score > flat.score


def test_the_other_vendors_revenue_consensus_is_not_consulted():
    """The mixing this rule forbids is just as wrong one leg down. A revenue
    actual from the feed measured against the calendar's revenue estimate is
    the same error in a bigger unit."""
    quality = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20],
                revenue_actual=1.05e9),
        a_consensus(revenue_avg=1.0e9, revenue_calendar=1.30e9), CONFIG)

    assert quality.revenue.status is LegStatus.BEAT
    assert quality.revenue.estimate == 1.0e9


def test_a_revenue_miss_is_reported_even_when_eps_beat():
    quality = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20],
                revenue_actual=0.9e9),
        a_consensus(), CONFIG)

    assert quality.revenue.status is LegStatus.MISS
    assert quality.verdict is QuarterVerdict.WEAK


# --- guidance: a bonus, never a gate --------------------------------------

def test_missing_guidance_costs_nothing():
    """Plenty of companies publish none, and there is no structured source
    for it anywhere — so absence is neutral (§5.3 決定3)."""
    without = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20],
                revenue_actual=1.05e9),
        a_consensus(next_quarter_eps_avg=2.00), CONFIG)

    assert without.guidance.status is LegStatus.ABSENT
    assert without.verdict is QuarterVerdict.GOOD_QUARTER
    assert without.score > 0


def test_guidance_above_consensus_earns_a_small_bonus():
    base = a_print(eps_actual=1.20, eps_actual_rows=[1.20],
                   revenue_actual=1.05e9)
    raised = base.model_copy(update={"guidance": GuidanceReading(
        period="2026-Q3", eps_low=2.10, eps_high=2.30,
        source_excerpt="expects Q3 EPS of $2.10 to $2.30")})

    without = assess_earnings(base, a_consensus(next_quarter_eps_avg=2.00),
                              CONFIG)
    with_guidance = assess_earnings(raised,
                                    a_consensus(next_quarter_eps_avg=2.00),
                                    CONFIG)

    assert with_guidance.guidance.status is LegStatus.BEAT
    assert (with_guidance.score - without.score
            == CONFIG.guidance_beat_score)


def test_full_year_guidance_is_never_scored_against_a_quarterly_consensus():
    """ADM guided FY2026 EPS of $5.15-$5.60 while the quarterly consensus sat
    near $1.20. Comparing the two reads as a +360% guidance beat that the
    company never gave — the numbers are simply for different periods.

    A full-year range is judged only against a full-year yardstick. With none
    captured, saying so is the only honest outcome (invariant 6).
    """
    quality = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20],
                revenue_actual=1.05e9,
                guidance=GuidanceReading(period="FY2026", eps_low=5.15,
                                         eps_high=5.60)),
        a_consensus(next_quarter_eps_avg=1.20), CONFIG)

    assert quality.guidance.status is LegStatus.ABSENT
    assert "no_full_year_consensus_to_compare" in quality.guidance.flags
    assert quality.guidance.surprise_pct is None


def test_full_year_guidance_is_scored_against_the_full_year_consensus():
    """The rescue this exists for. ADM guided FY2026 EPS of $5.15-$5.60 and
    the analysts' figure for the same year — stated in the same summary
    sentence — was $4.76. That is a real beat, and it was previously recorded
    as "no guidance published"."""
    quality = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20],
                revenue_actual=1.05e9,
                guidance=GuidanceReading(period="FY2026", eps_low=5.15,
                                         eps_high=5.60)),
        a_consensus(next_quarter_eps_avg=1.20, full_year_eps_avg=4.76,
                    full_year_period="FY2026"), CONFIG)

    assert quality.guidance.status is LegStatus.BEAT
    assert quality.guidance.estimate == 4.76          # never the 1.20
    assert round(quality.guidance.surprise_pct, 1) == 12.9
    assert "on_eps" in quality.guidance.flags


def test_a_full_year_yardstick_for_another_year_is_refused():
    """Same shape of error as the quarterly one, one period up: FY2027
    guidance measured against a FY2026 consensus is a comparison nobody made.
    """
    quality = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20],
                revenue_actual=1.05e9,
                guidance=GuidanceReading(period="FY2027", eps_low=5.15,
                                         eps_high=5.60)),
        a_consensus(full_year_eps_avg=4.76, full_year_period="FY2026"), CONFIG)

    assert quality.guidance.status is LegStatus.ABSENT
    assert "full_year_consensus_is_another_year" in quality.guidance.flags
    assert quality.guidance.surprise_pct is None


def test_an_unlabelled_full_year_yardstick_is_not_trusted():
    """A figure whose year nobody stated could be any year. Fail closed."""
    quality = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20],
                revenue_actual=1.05e9,
                guidance=GuidanceReading(period="FY2026", eps_low=5.15,
                                         eps_high=5.60)),
        a_consensus(full_year_eps_avg=4.76), CONFIG)

    assert quality.guidance.status is LegStatus.ABSENT
    assert "no_full_year_consensus_to_compare" in quality.guidance.flags


def test_full_year_revenue_guidance_uses_the_full_year_revenue_yardstick():
    """The common case in the corpus: 14 of the 18 names that state a
    full-year consensus state it on revenue, not EPS."""
    quality = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20],
                revenue_actual=1.05e9,
                guidance=GuidanceReading(period="FY2026",
                                         revenue_low=5.70e9,
                                         revenue_high=6.00e9)),
        a_consensus(full_year_revenue_avg=5.80e9,
                    full_year_period="FY2026"), CONFIG)

    assert quality.guidance.status is LegStatus.BEAT
    assert quality.guidance.estimate == 5.80e9
    assert "on_revenue" in quality.guidance.flags


def test_guidance_for_a_quarter_other_than_the_next_one_is_not_compared():
    quality = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20],
                revenue_actual=1.05e9,
                guidance=GuidanceReading(period="2027-Q1", eps_low=2.10,
                                         eps_high=2.30)),
        a_consensus(next_quarter_eps_avg=2.00), CONFIG)

    assert quality.guidance.status is LegStatus.ABSENT
    assert "guidance_period_not_comparable" in quality.guidance.flags


def test_guidance_with_no_period_label_is_still_compared():
    """Readings recorded before the period was carried have no label.
    Refusing those would drop guidance this system already holds, so an
    unlabelled range keeps its old meaning: it is taken as next quarter's."""
    quality = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20],
                revenue_actual=1.05e9,
                guidance=GuidanceReading(eps_low=2.10, eps_high=2.30)),
        a_consensus(next_quarter_eps_avg=2.00), CONFIG)

    assert quality.guidance.status is LegStatus.BEAT


def test_revenue_guidance_counts_when_the_company_gives_no_eps_range():
    """Amazon guides on net sales and operating income, never on EPS. Judging
    guidance only on EPS would score every such company as "no guidance",
    which is a fact about our reading rather than about the company."""
    quality = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20],
                revenue_actual=1.05e9,
                guidance=GuidanceReading(period="2026-Q3",
                                         revenue_low=1.10e9,
                                         revenue_high=1.16e9)),
        a_consensus(next_quarter_revenue_avg=1.08e9), CONFIG)

    assert quality.guidance.status is LegStatus.BEAT
    assert quality.guidance.leg == "guidance"
    assert round(quality.guidance.surprise_pct, 1) == 4.6


def test_guidance_below_consensus_is_reported_without_a_mechanical_penalty():
    """The Adversary is free to attack a cut; the screen does not dock
    points for one, because "no guidance" and "weak guidance" must not be
    scored on the same axis as the two legs that have real consensus."""
    cut = a_print(eps_actual=1.20, eps_actual_rows=[1.20],
                  revenue_actual=1.05e9,
                  guidance=GuidanceReading(period="2026-Q3", eps_low=1.60,
                                           eps_high=1.80))
    without = assess_earnings(
        cut.model_copy(update={"guidance": None}),
        a_consensus(next_quarter_eps_avg=2.00), CONFIG)
    lowered = assess_earnings(cut, a_consensus(next_quarter_eps_avg=2.00),
                              CONFIG)

    assert lowered.guidance.status is LegStatus.MISS
    assert lowered.score == without.score
    assert lowered.verdict is QuarterVerdict.MIXED


# --- the whole quarter ------------------------------------------------------

def test_all_three_legs_beating_is_a_good_quarter():
    quality = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20],
                revenue_actual=1.05e9,
                guidance=GuidanceReading(period="2026-Q3", eps_low=2.10,
                                         eps_high=2.30)),
        a_consensus(next_quarter_eps_avg=2.00), CONFIG)

    assert quality.verdict is QuarterVerdict.GOOD_QUARTER
    assert [leg.status for leg in (quality.eps, quality.revenue,
                                   quality.guidance)] == [LegStatus.BEAT] * 3


def test_a_leg_that_missed_cannot_score_like_a_leg_with_no_data():
    """Found on the live AMZN dry run (2026-08-02): a +194% EPS beat whose
    revenue MISSED still scored the capped maximum, outranking a name that
    beat on both. A miss is a fact about the quarter, and the user's own
    definition of a good quarter requires all three legs — so it subtracts,
    mirroring the bonus a beat earns, rather than reading as absent data."""
    eps_only = assess_earnings(
        a_print(eps_actual=3.00, eps_actual_rows=[3.00],
                revenue_actual=0.9e9),
        a_consensus(), CONFIG)
    both_legs = assess_earnings(
        a_print(eps_actual=1.40, eps_actual_rows=[1.40],
                revenue_actual=1.08e9),
        a_consensus(), CONFIG)

    assert eps_only.eps.surprise_pct > both_legs.eps.surprise_pct
    assert eps_only.score < both_legs.score


def test_a_missing_revenue_reading_is_not_a_penalty():
    """The mirror of the rule above: no revenue data is unverified, and
    unverified scores zero — it must never be scored as a miss."""
    no_data = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20]),
        a_consensus(revenue_avg=None, revenue_calendar=None), CONFIG)
    missed = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20],
                revenue_actual=0.9e9),
        a_consensus(), CONFIG)

    assert no_data.score > missed.score


def test_a_miss_the_chosen_vendor_establishes_still_subtracts():
    quality = assess_earnings(
        a_print(eps_actual=0.80, eps_actual_rows=[0.80]),
        a_consensus(eps_avg=1.00, eps_calendar=1.00), CONFIG)

    assert quality.eps.status is LegStatus.MISS
    assert quality.score < 0.0


def test_the_event_day_reaction_still_shapes_the_score():
    """The three legs describe the print; the ranking also has to prefer a
    reaction that confirms it without exhausting it — that term is unchanged
    and still applies."""
    confirmed = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20]), a_consensus(), CONFIG,
        gap_on_event_pct=6.0)
    rejected = assess_earnings(
        a_print(eps_actual=1.20, eps_actual_rows=[1.20]), a_consensus(), CONFIG,
        gap_on_event_pct=-5.0)

    assert confirmed.score > rejected.score
