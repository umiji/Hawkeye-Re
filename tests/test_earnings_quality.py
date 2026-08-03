"""Judging a quarter on three legs (docs/design/MASTER_OVERVIEW.ja.md §5.3).

Every case here is a name that was actually measured on 2026-08-02, because
the whole design came out of those measurements rather than out of theory:
AMZN's +215% that is really ~+7%, AAPL's two correct-but-different actuals,
BIIB's consensus that moved 3.98 -> 2.15, INVH's consensus built from one
analyst.

The rule the tests defend: a beat is only a beat when BOTH sources say so,
and everything that cannot be confirmed scores zero rather than passing
quietly.
"""
from __future__ import annotations

import dataclasses
from datetime import date

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    EarningsPrint,
    EpsBasis,
    GuidanceReading,
    PrintDepth,
    SnapshotKind,
)
from hawkeye.scout.quality import LegStatus, QuarterVerdict, assess_earnings

CONFIG = HawkeyeConfig()


def a_print(**overrides) -> EarningsPrint:
    base = dict(stock_id="cik:0001018724", ticker="TEST",
                fiscal_quarter="2026-Q2", report_date=date(2026, 7, 31),
                depth=PrintDepth.VERIFIED)
    base.update(overrides)
    return EarningsPrint(**base)


def a_consensus(**overrides) -> ConsensusSnapshot:
    base = dict(stock_id="cik:0001018724", ticker="TEST",
                fiscal_quarter="2026-Q2", kind=SnapshotKind.PRE_REGISTERED,
                eps_avg=1.00, eps_finnhub=1.00, eps_analysts=20,
                revenue_avg=1.0e9, revenue_finnhub=1.0e9, revenue_analysts=18)
    base.update(overrides)
    return ConsensusSnapshot(**base)


# --- EPS: a beat needs both sources ---------------------------------------

def test_both_sources_agreeing_on_a_beat_is_a_beat():
    quality = assess_earnings(
        a_print(eps_yahoo=1.20, eps_finnhub=[1.20]),
        a_consensus(eps_avg=1.00, eps_finnhub=1.00), CONFIG)

    assert quality.eps.status is LegStatus.BEAT
    assert quality.eps.surprise_pct == 20.0
    assert quality.eps.sources == 2


def test_a_beat_on_one_consensus_and_a_miss_on_the_other_is_not_a_beat():
    """TBBK / PCAR / CARR / TRS sat exactly here on the measured week: the
    consensus figures differ by 1-2% across the screen's threshold, and no
    precision in the actual can settle it — consensus has no primary source
    (§5.3(7))."""
    quality = assess_earnings(
        a_print(eps_yahoo=1.00, eps_finnhub=[1.00]),
        a_consensus(eps_avg=0.98, eps_finnhub=1.02), CONFIG)

    assert quality.eps.status is LegStatus.INLINE
    assert quality.eps.surprise_pct < 0            # ranked on the conservative side
    assert "sources_disagree_on_direction" in quality.eps.flags


def test_a_disputed_consensus_is_ranked_on_the_conservative_reading():
    """BIIB: 3.98 vs 2.15. Ranked at +20%, not +77% — the same
    collapse-to-conservative rule already applied to Finnhub's duplicate
    rows, extended across vendors (§5.3 決定4)."""
    quality = assess_earnings(
        a_print(eps_yahoo=2.58, eps_finnhub=[2.58]),
        a_consensus(eps_avg=2.15, eps_finnhub=3.98), CONFIG)

    assert "consensus_disputed" in quality.eps.flags
    assert quality.eps.status is LegStatus.INLINE          # one source says miss
    assert round(quality.eps.surprise_pct, 1) == -35.2


def test_disagreeing_actuals_are_judged_on_the_smaller_of_the_two():
    """AAPL: Yahoo 2.02 (matches the filing), Finnhub 1.91 (= 2.02 - 0.11 of
    tariff refunds). Neither is wrong; the bases differ.

    Doctrine change of 2026-08-03(b) (`earnings_actual_dispute_blocks`): the
    dispute no longer makes the leg unverified. Judging it on the SMALLER
    actual against the LARGER consensus means the beat holds under either
    vendor's reading of either number — which is strictly stronger evidence
    than the single-source actual this system already accepts. The flag stays
    on the leg and the release is still asked for.
    """
    quality = assess_earnings(
        a_print(eps_yahoo=2.02, eps_finnhub=[1.91]),
        a_consensus(eps_avg=1.89, eps_finnhub=1.89), CONFIG)

    assert quality.eps.status is LegStatus.BEAT
    assert quality.eps.actual == 1.91                   # the conservative one
    assert round(quality.eps.surprise_pct, 2) == 1.06
    assert "actual_disputed" in quality.eps.flags
    assert quality.score > 0.0


def test_a_dispute_that_only_the_larger_reading_survives_is_not_a_beat():
    """The conservative rule has to bite, not merely be stated: the smaller
    actual lands below consensus, so no beat is established.

    Nor is a MISS. A beat and a miss are not symmetric under a dispute — the
    leg is judged on the smaller actual, so a beat under it holds under the
    larger one too, while a miss under it says nothing about the larger one
    (a company that took a CHARGE shows AAPL's gap in reverse). Calling it a
    miss would SUBTRACT points, i.e. our inability to tell which figure is
    comparable would read as the company having done badly.
    """
    quality = assess_earnings(
        a_print(eps_yahoo=1.30, eps_finnhub=[0.95]),
        a_consensus(eps_avg=1.00, eps_finnhub=1.00), CONFIG)

    assert quality.eps.actual == 0.95
    assert quality.eps.status is LegStatus.UNVERIFIED
    assert "actual_disputed" in quality.eps.flags
    assert quality.score == 0.0


def test_a_miss_both_vendors_agree_on_still_subtracts():
    """The other half of the same rule: when even the LARGER actual missed,
    the miss is established and costs what a beat of that size would earn."""
    quality = assess_earnings(
        a_print(eps_yahoo=0.80, eps_finnhub=[0.60]),
        a_consensus(eps_avg=1.00, eps_finnhub=1.00), CONFIG)

    assert quality.eps.status is LegStatus.MISS
    assert "actual_disputed" in quality.eps.flags
    assert quality.score < 0.0


def test_a_disputed_actual_is_still_worth_reading_the_release_for():
    """Scoring the leg conservatively is not the same as settling it. The
    document is what turns "true under both readings" into "this is the
    number", and it also fixes the basis — so the escalation must survive
    the doctrine change that stopped the dispute blocking."""
    from hawkeye.scout.release import needs_release_read

    quality = assess_earnings(
        a_print(eps_yahoo=2.02, eps_finnhub=[1.91]),
        a_consensus(eps_avg=1.89, eps_finnhub=1.89), CONFIG)

    assert needs_release_read(quality)


def test_the_tribunal_is_told_the_two_sources_disagreed():
    """The prompts tell the Bull and the Adversary to prefer structured
    numbers over prose, so a figure that is now allowed to score has to carry
    its weakness in the same place they are looking."""
    from hawkeye.scout.quality import describe_quality_en

    text = describe_quality_en(assess_earnings(
        a_print(eps_yahoo=2.02, eps_finnhub=[1.91]),
        a_consensus(eps_avg=1.89, eps_finnhub=1.89), CONFIG))

    assert "actual_disputed" in text
    assert "2.02" in text and "1.91" in text
    assert "conservative" in text.lower()


def test_the_old_blocking_behaviour_is_one_config_flag_away():
    """A doctrine change is a config diff (invariant 7). Flipping it back has
    to restore the previous judgment exactly, or the flag is decoration."""
    blocking = dataclasses.replace(CONFIG, earnings_actual_dispute_blocks=True)

    quality = assess_earnings(
        a_print(eps_yahoo=2.02, eps_finnhub=[1.91]),
        a_consensus(eps_avg=1.89, eps_finnhub=1.89), blocking)

    assert quality.eps.status is LegStatus.UNVERIFIED
    assert quality.score == 0.0


def test_a_leg_with_no_actual_at_all_is_still_unverified():
    """The change is narrow. Invariant 6 is about MISSING data, and missing
    is still missing: two readings that disagree are more information than
    one, but no readings are none."""
    quality = assess_earnings(
        a_print(), a_consensus(eps_avg=1.00, eps_finnhub=1.00), CONFIG)

    assert quality.eps.status is LegStatus.UNVERIFIED
    assert "no_actual" in quality.eps.flags


def test_a_penny_of_rounding_is_not_a_dispute():
    quality = assess_earnings(
        a_print(eps_yahoo=1.11, eps_finnhub=[1.10]),
        a_consensus(eps_avg=1.00, eps_finnhub=1.00), CONFIG)

    assert "actual_disputed" not in quality.eps.flags
    assert quality.eps.status is LegStatus.BEAT
    assert quality.eps.surprise_pct == 10.0        # the conservative actual


def test_the_release_settles_a_dispute_and_records_the_basis():
    """Reading the release resolves AAPL to 1.91 on an adjusted basis. The
    figure is READ, never computed (§5.3 決定1)."""
    quality = assess_earnings(
        a_print(eps_yahoo=2.02, eps_finnhub=[1.91], eps_release=1.91,
                eps_basis=EpsBasis.ADJUSTED, one_off_per_share=0.11,
                depth=PrintDepth.RELEASE_READ),
        a_consensus(eps_avg=1.89, eps_finnhub=1.89), CONFIG)

    assert quality.eps.status is LegStatus.BEAT
    assert round(quality.eps.surprise_pct, 2) == 1.06
    assert "actual_disputed" not in quality.eps.flags
    assert quality.eps.basis is EpsBasis.ADJUSTED


def test_finnhubs_contradictory_rows_make_its_actual_unusable():
    """AMZN's calendar returned 1.88 AND 1.97 for one print. Picking the row
    that happens to match Yahoo would be exactly the "choose the more
    plausible one" judgment this system exists to remove — so Finnhub simply
    contributes no actual, and the reading stands on one source."""
    quality = assess_earnings(
        a_print(eps_yahoo=5.75, eps_finnhub=[1.88, 1.97],
                eps_basis=EpsBasis.UNADJUSTED),
        a_consensus(eps_avg=1.83, eps_finnhub=1.83), CONFIG)

    assert "finnhub_actual_conflict" in quality.eps.flags
    assert "single_source_actual" in quality.eps.flags
    assert quality.eps.status is LegStatus.BEAT       # both CONSENSUS agree
    assert "unadjusted" in quality.flags              # the one-off warning
    assert quality.score <= 70.0                      # capped, never +215


def test_a_consensus_from_too_few_analysts_is_unverified():
    """INVH's EPS consensus was built from ONE analyst, and nothing in the
    vendor response says so — only the pre-registered Yahoo row does."""
    quality = assess_earnings(
        a_print(eps_yahoo=1.50, eps_finnhub=[1.50]),
        a_consensus(eps_avg=1.00, eps_finnhub=1.00, eps_analysts=1), CONFIG)

    assert quality.eps.status is LegStatus.UNVERIFIED
    assert "thin_coverage" in quality.eps.flags


def test_a_near_zero_consensus_cannot_buy_a_ranking_slot():
    """REITs report FFO, so their GAAP EPS consensus sits near zero and the
    ratio measures the denominator, not the beat (the existing guard)."""
    quality = assess_earnings(
        a_print(eps_yahoo=0.30, eps_finnhub=[0.30]),
        a_consensus(eps_avg=0.02, eps_finnhub=0.02), CONFIG)

    assert quality.eps.status is LegStatus.UNVERIFIED
    assert "estimate_too_small" in quality.eps.flags
    assert quality.score == 0.0


def test_only_one_consensus_source_cannot_confirm_a_beat():
    """Without a pre-registered Yahoo row there is one opinion, and one
    opinion cannot satisfy "both sources agree" — it is recorded and passed
    on as unverified rather than treated as a beat."""
    quality = assess_earnings(
        a_print(eps_yahoo=1.20, eps_finnhub=[1.20]),
        a_consensus(eps_avg=None, eps_analysts=None, eps_finnhub=1.00), CONFIG)

    assert quality.eps.status is LegStatus.UNVERIFIED
    assert "single_source_consensus" in quality.eps.flags
    assert quality.eps.surprise_pct == 20.0        # still reported


def test_no_consensus_at_all_is_absent_not_a_beat():
    quality = assess_earnings(a_print(eps_yahoo=1.20), None, CONFIG)
    assert quality.eps.status is LegStatus.UNVERIFIED
    assert quality.verdict is QuarterVerdict.UNVERIFIED


# --- revenue ---------------------------------------------------------------

def test_revenue_beats_on_both_sources_add_to_the_score():
    beat = assess_earnings(
        a_print(eps_yahoo=1.20, eps_finnhub=[1.20], revenue_xbrl=1.05e9,
                revenue_finnhub=1.05e9),
        a_consensus(revenue_avg=1.0e9, revenue_finnhub=1.0e9), CONFIG)
    flat = assess_earnings(
        a_print(eps_yahoo=1.20, eps_finnhub=[1.20]),
        a_consensus(revenue_avg=None, revenue_finnhub=None), CONFIG)

    assert beat.revenue.status is LegStatus.BEAT
    assert round(beat.revenue.surprise_pct, 1) == 5.0
    assert beat.score > flat.score


def test_a_revenue_miss_is_reported_even_when_eps_beat():
    quality = assess_earnings(
        a_print(eps_yahoo=1.20, eps_finnhub=[1.20], revenue_xbrl=0.9e9,
                revenue_finnhub=0.9e9),
        a_consensus(), CONFIG)

    assert quality.revenue.status is LegStatus.MISS
    assert quality.verdict is QuarterVerdict.WEAK


# --- guidance: a bonus, never a gate --------------------------------------

def test_missing_guidance_costs_nothing():
    """Plenty of companies publish none, and there is no structured source
    for it anywhere — so absence is neutral (§5.3 決定3)."""
    without = assess_earnings(
        a_print(eps_yahoo=1.20, eps_finnhub=[1.20], revenue_xbrl=1.05e9,
                revenue_finnhub=1.05e9),
        a_consensus(next_quarter_eps_avg=2.00), CONFIG)

    assert without.guidance.status is LegStatus.ABSENT
    assert without.verdict is QuarterVerdict.GOOD_QUARTER
    assert without.score > 0


def test_guidance_above_consensus_earns_a_small_bonus():
    base = a_print(eps_yahoo=1.20, eps_finnhub=[1.20], revenue_xbrl=1.05e9,
                   revenue_finnhub=1.05e9)
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


def test_revenue_guidance_counts_when_the_company_gives_no_eps_range():
    """Amazon guides on net sales and operating income, never on EPS. Judging
    guidance only on EPS would score every such company as "no guidance",
    which is a fact about our reading rather than about the company."""
    quality = assess_earnings(
        a_print(eps_yahoo=1.20, eps_finnhub=[1.20], revenue_xbrl=1.05e9,
                revenue_finnhub=1.05e9,
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
    cut = a_print(eps_yahoo=1.20, eps_finnhub=[1.20], revenue_xbrl=1.05e9,
                  revenue_finnhub=1.05e9,
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
        a_print(eps_yahoo=1.20, eps_finnhub=[1.20], revenue_xbrl=1.05e9,
                revenue_finnhub=1.05e9,
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
        a_print(eps_yahoo=3.00, eps_finnhub=[3.00], revenue_xbrl=0.9e9,
                revenue_finnhub=0.9e9),
        a_consensus(), CONFIG)
    both_legs = assess_earnings(
        a_print(eps_yahoo=1.40, eps_finnhub=[1.40], revenue_xbrl=1.08e9,
                revenue_finnhub=1.08e9),
        a_consensus(), CONFIG)

    assert eps_only.eps.surprise_pct > both_legs.eps.surprise_pct
    assert eps_only.score < both_legs.score


def test_a_missing_revenue_reading_is_not_a_penalty():
    """The mirror of the rule above: no revenue data is unverified, and
    unverified scores zero — it must never be scored as a miss."""
    no_data = assess_earnings(
        a_print(eps_yahoo=1.20, eps_finnhub=[1.20]),
        a_consensus(revenue_avg=None, revenue_finnhub=None), CONFIG)
    missed = assess_earnings(
        a_print(eps_yahoo=1.20, eps_finnhub=[1.20], revenue_xbrl=0.9e9,
                revenue_finnhub=0.9e9),
        a_consensus(), CONFIG)

    assert no_data.score > missed.score


def test_the_event_day_reaction_still_shapes_the_score():
    """The three legs describe the print; the ranking also has to prefer a
    reaction that confirms it without exhausting it — that term is unchanged
    and still applies."""
    confirmed = assess_earnings(
        a_print(eps_yahoo=1.20, eps_finnhub=[1.20]), a_consensus(), CONFIG,
        gap_on_event_pct=6.0)
    rejected = assess_earnings(
        a_print(eps_yahoo=1.20, eps_finnhub=[1.20]), a_consensus(), CONFIG,
        gap_on_event_pct=-5.0)

    assert confirmed.score > rejected.score
