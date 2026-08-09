"""The whisper bonus (docs/backlog/PIPELINE_BUILD_TASKS.ja.md task 8).

The feed publishes a second expectation beside the analyst consensus: the
"whisper" number, an unofficial figure that is usually HIGHER than consensus
(11 of 11 measured). Clearing it is therefore a stricter test than clearing
consensus, and the ranking treats it that way — as a small bonus on top of the
consensus-based surprise, never as a replacement for it.

Three rules the tests defend:

1. **Additive, never substitutive.** The denominator of the EPS surprise stays
   the consensus. Swapping in the whisper would lower every surprise ratio, and
   a bonus paid on top of a silently reduced base cannot be told apart from a
   bonus that is merely refilling what the swap took away.
2. **Asymmetric.** Beating the whisper adds; falling short of it subtracts
   nothing. The evidence behind the signal is contested (Bagnoli/Beneish/Watts
   1999 predates Reg FD and later work reports the effect reversing), and the
   consensus leg already penalises a miss symmetrically. Penalising twice on a
   contested signal would weight it above its evidence.
3. **One vendor on both sides.** The bonus compares the feed's actual against
   the feed's whisper. When the calendar supplied the actual there is no
   comparable whisper, and no bonus is paid — the same rule that governs the
   surprise ratio itself (task 7.5).
"""
from __future__ import annotations

from datetime import date

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    EarningsPrint,
    PrintSource,
    SnapshotKind,
)
from hawkeye.scout.quality import assess_earnings

CONFIG = HawkeyeConfig()


def a_print(**overrides) -> EarningsPrint:
    base = dict(stock_id="cik:0001018724", ticker="TEST",
                fiscal_quarter="2026-Q2", report_date=date(2026, 7, 31),
                source=PrintSource.WHISPERS,
                eps_actual=1.20, eps_actual_rows=[1.20],
                revenue_actual=1.0e9)
    base.update(overrides)
    return EarningsPrint(**base)


def a_consensus(**overrides) -> ConsensusSnapshot:
    base = dict(stock_id="cik:0001018724", ticker="TEST",
                fiscal_quarter="2026-Q2", kind=SnapshotKind.PRE_REGISTERED,
                eps_avg=1.00, eps_calendar=1.00, eps_analysts=20,
                revenue_avg=1.0e9, revenue_calendar=1.0e9, revenue_analysts=18)
    base.update(overrides)
    return ConsensusSnapshot(**base)


# --- the bonus itself ------------------------------------------------------

def test_clearing_the_whisper_outranks_merely_meeting_it():
    """Both prints beat consensus by the same 20%. The one that also cleared
    the unofficial figure ranks higher."""
    cleared = assess_earnings(a_print(), a_consensus(eps_whisper=1.10), CONFIG)
    met = assess_earnings(a_print(), a_consensus(eps_whisper=1.20), CONFIG)

    assert cleared.eps.surprise_pct == met.eps.surprise_pct == 20.0
    assert cleared.score > met.score


def test_falling_short_of_the_whisper_costs_nothing():
    """Asymmetric by design (rule 2). A print that beat consensus but came in
    under the whisper scores exactly what it would with no whisper at all."""
    short = assess_earnings(a_print(), a_consensus(eps_whisper=1.30), CONFIG)
    absent = assess_earnings(a_print(), a_consensus(eps_whisper=None), CONFIG)

    assert short.score == absent.score


def test_the_bonus_is_capped():
    """A whisper close to zero produces an enormous percentage over it. The
    cap is what stops that arithmetic from buying a ranking slot."""
    enormous = assess_earnings(a_print(), a_consensus(eps_whisper=0.01), CONFIG)
    absent = assess_earnings(a_print(), a_consensus(eps_whisper=None), CONFIG)

    assert enormous.score - absent.score == CONFIG.whisper_beat_cap


def test_a_calendar_backed_print_earns_no_whisper_bonus():
    """The calendar supplied this actual, so there is no same-vendor pair to
    compare (rule 3)."""
    from_calendar = assess_earnings(
        a_print(source=PrintSource.FINNHUB, eps_actual=1.20),
        a_consensus(eps_whisper=1.10), CONFIG)
    absent = assess_earnings(
        a_print(source=PrintSource.FINNHUB, eps_actual=1.20),
        a_consensus(eps_whisper=None), CONFIG)

    assert from_calendar.score == absent.score


def test_an_unverified_eps_leg_earns_no_whisper_bonus():
    """Unverified earns nothing, everywhere (invariant 6). A leg whose own
    surprise could not be trusted must not collect a bonus on the side."""
    unverified = assess_earnings(
        a_print(), a_consensus(eps_avg=None, eps_analysts=None,
                               eps_whisper=1.10), CONFIG)

    assert unverified.score == 0.0


def test_the_weight_lives_in_config():
    """Doctrine numbers are a config diff (invariant 7), so setting the weight
    to zero has to switch the whole mechanism off."""
    off = HawkeyeConfig(whisper_beat_weight=0.0)
    with_bonus = assess_earnings(a_print(), a_consensus(eps_whisper=1.10), off)
    absent = assess_earnings(a_print(), a_consensus(eps_whisper=None), off)

    assert with_bonus.score == absent.score


# --- the number has to actually arrive -------------------------------------
#
# A bonus nothing supplies a number to is a bonus that never fires. Both rows
# that can carry a whisper are checked here: the one written before the print
# and the one reconstructed after it.

def test_the_pre_registered_row_carries_the_whisper(tmp_path):
    """Written before the release, from the feed's forward endpoint."""
    from datetime import datetime, timedelta, timezone

    from hawkeye.ledger.stocks import StockStore
    from hawkeye.marketdata.whispers import WhispersForecast
    from hawkeye.scout.prereg import UpcomingPrint, capture_consensus

    class StubForecast:
        def forecast(self, ticker):
            return WhispersForecast(ticker=ticker, eps_estimate=1.83,
                                    revenue_estimate=1.62e11, whisper=1.95,
                                    next_report_date=date(2026, 8, 3))

    store = StockStore(str(tmp_path / "hawkeye.db"))
    prints = [UpcomingPrint(ticker="AMZN", report_date=date(2026, 8, 3),
                            fiscal_quarter="2026-Q2", eps_estimate=1.83,
                            revenue_estimate=1.62e11)]
    capture_consensus(store, prints, StubForecast(),
                      captured_at=datetime(2026, 8, 2, 9,
                                           tzinfo=timezone(timedelta(hours=9))))

    stock_id = store.stock_by_ticker("AMZN").id
    assert store.consensus_in_force(stock_id, "2026-Q2").eps_whisper == 1.95


def test_the_reconstructed_row_carries_the_whisper():
    """Read off the print's own response afterwards. Governed by the same
    one-vendor rule as the surprise ratio: a whisper is only recorded when the
    feed is also the vendor the figures stand on."""
    from hawkeye.scout.earnings import EarningsEvent
    from hawkeye.scout.quality import reconstructed_consensus

    from_feed = reconstructed_consensus(
        EarningsEvent(ticker="AMD", day=date(2026, 8, 4), eps_actual=1.20,
                      eps_estimate=1.00, revenue_actual=1.1e9,
                      revenue_estimate=1.0e9, numbers_source="whispers",
                      whisper=1.10), "cik:1")
    from_calendar = reconstructed_consensus(
        EarningsEvent(ticker="AMD", day=date(2026, 8, 4), eps_actual=1.20,
                      eps_estimate=1.00, revenue_actual=1.1e9,
                      revenue_estimate=1.0e9, numbers_source="calendar",
                      whisper=1.10), "cik:1")

    assert from_feed.eps_whisper == 1.10
    assert from_calendar.eps_whisper is None


def test_the_numbers_rule_carries_the_whisper_off_the_feeds_answer():
    """The link that makes the whole mechanism fire in production: the number
    exists on the feed's response, and the step that substitutes the feed's
    figures is the only thing that can put it on the event."""
    from datetime import datetime

    from hawkeye.marketdata.whispers import EASTERN, WhispersRecord
    from hawkeye.scout.earnings import EarningsEvent
    from hawkeye.scout.numbers import read_numbers

    class Feed:
        def details(self, ticker):
            return WhispersRecord(
                ticker=ticker, name=ticker, quarter_end=date(2026, 6, 30),
                fiscal_quarter="2026-Q2", year_end=date(2026, 12, 31),
                announced_at=datetime(2026, 7, 30, 16, 5, tzinfo=EASTERN),
                eps_actual=1.30, eps_consensus=1.00,
                eps_consensus_high=None, eps_consensus_low=None,
                revenue_actual=1.05e9, revenue_consensus=1.00e9,
                whisper=1.10)

    event = EarningsEvent(ticker="AAA", day=date(2026, 7, 30), eps_actual=1.20,
                          eps_estimate=1.00, revenue_actual=1.02e9,
                          revenue_estimate=1.00e9)
    read, _ = read_numbers([event], [], Feed(), limit=1,
                           always=[("AAA", date(2026, 7, 30))])

    assert read[0].numbers_source == "whispers"
    assert read[0].whisper == 1.10


# --- the reader can see what moved the score -------------------------------

def test_the_verdict_records_the_whisper_and_the_beat():
    """A term that moves the ranking must not be invisible: the score alone
    cannot tell the reader that 2.8 of its points came from clearing an
    unofficial figure they were never shown."""
    quality = assess_earnings(a_print(), a_consensus(eps_whisper=1.10), CONFIG)

    assert quality.whisper == 1.10
    assert round(quality.whisper_beat_pct, 1) == 9.1


def test_the_japanese_report_names_the_whisper_it_cleared():
    from hawkeye.reports.quality_ja import render_quality_ja

    text = render_quality_ja(
        assess_earnings(a_print(), a_consensus(eps_whisper=1.10), CONFIG))
    silent = render_quality_ja(
        assess_earnings(a_print(), a_consensus(eps_whisper=None), CONFIG))

    assert "1.1" in text and "+9.1%" in text
    assert "囁き" in text                       # named, not left as a number
    assert "囁き" not in silent                 # nothing to say, nothing said
