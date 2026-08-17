"""Sector comparison material for the tribunal.

The three roles could see how far the candidate moved on its catalyst but
had nothing to compare it against, so a stock that gapped 8% while its
whole sector gapped 7% read exactly like one that gapped 8% on its own.
These tests pin the sector -> ETF resolution and the relative-move
arithmetic that closes that gap.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from hawkeye.contracts.models import Catalyst, CatalystType, GateReport
from hawkeye.tribunal import casefile
from hawkeye.marketdata.base import Bar
from hawkeye.marketdata.sector_etf import (
    SECTOR_ETFS,
    etf_for_industry,
    sector_for_industry,
)
from hawkeye.marketdata.snapshot import build_brief, build_sector_context
from hawkeye.tribunal.prompts import (
    render_adversary_input,
    render_bull_input,
    render_judge_input,
)
from tests.conftest import make_snapshot

# Every distinct `finnhubIndustry` value the system has actually received,
# measured 2026-08-17 over the 19 recommendations in
# var/legacy/hawkeye-20260806-preEW.db (see docs/knowledge/MEASUREMENTS.ja.md).
# This list is the reason the mapping table exists in the shape it does —
# if a live industry stops resolving, this test is what says so.
OBSERVED_FINNHUB_INDUSTRIES = [
    "Health Care",
    "Technology",
    "Real Estate",
    "Hotels, Restaurants & Leisure",
    "Semiconductors",
    "Consumer products",
    "Financial Services",
    "Insurance",
    "Energy",
    "Biotechnology",
    "Textiles, Apparel & Luxury Goods",
    "Retail",
    "Machinery",
]

EVENT_DAY = date(2026, 8, 10)


def _bars_with_event(pre: float, on_event: float, last: float) -> list[Bar]:
    """Three closes: the day before the catalyst, the catalyst day, and the
    latest bar. event_stats() reads exactly these three points."""
    days = [EVENT_DAY - timedelta(days=1), EVENT_DAY, EVENT_DAY + timedelta(days=3)]
    return [Bar(day=d, open=c, high=c, low=c, close=c, volume=1e6)
            for d, c in zip(days, (pre, on_event, last))]


class ByTickerProvider:
    """Returns different history per ticker, which StaticProvider cannot —
    the whole point here is that the candidate and its ETF moved differently."""

    def __init__(self, bars_by_ticker: dict, profile_data: dict | None = None):
        self.bars_by_ticker = bars_by_ticker
        self.profile_data = profile_data or {}
        self.asked: list[str] = []

    def daily_history(self, ticker: str, days: int = 365) -> list[Bar]:
        self.asked.append(ticker)
        answer = self.bars_by_ticker.get(ticker)
        if isinstance(answer, Exception):
            raise answer
        return answer or []

    def profile(self, ticker: str) -> dict:
        return self.profile_data

    def news(self, ticker: str, limit: int = 10):
        return []


# ---------------------------------------------------------------------------
# The mapping table
# ---------------------------------------------------------------------------

def test_all_eleven_gics_sectors_have_a_distinct_etf():
    assert len(SECTOR_ETFS) == 11
    assert len(set(SECTOR_ETFS.values())) == 11


@pytest.mark.parametrize("industry", OBSERVED_FINNHUB_INDUSTRIES)
def test_every_industry_seen_live_resolves_to_an_etf(industry):
    resolved = etf_for_industry(industry)
    assert resolved is not None, f"{industry!r} no longer resolves"
    sector, etf = resolved
    assert SECTOR_ETFS[sector] == etf


def test_industries_of_the_same_sector_share_one_etf():
    # Two Finnhub labels, one GICS sector — this is what the table is for.
    assert sector_for_industry("Biotechnology") == sector_for_industry("Health Care")
    assert sector_for_industry("Semiconductors") == sector_for_industry("Technology")
    assert sector_for_industry("Insurance") == sector_for_industry("Financial Services")


def test_lookup_ignores_case_and_surrounding_whitespace():
    assert sector_for_industry("  SEMICONDUCTORS ") == sector_for_industry("Semiconductors")


def test_a_gics_sector_name_resolves_as_well_as_a_finnhub_label():
    """The dossier's `sector` is whatever the provider said. Finnhub says
    "Technology"; a provider that says "Information Technology" must not
    fall off the table for it."""
    assert etf_for_industry("Information Technology") == ("Information Technology", "XLK")


def test_an_unknown_industry_resolves_to_nothing_rather_than_a_guess():
    assert sector_for_industry("Blockchain Metaverse Holdings") is None
    assert etf_for_industry("Blockchain Metaverse Holdings") is None


def test_a_missing_sector_resolves_to_nothing():
    assert etf_for_industry("") is None
    assert etf_for_industry("   ") is None


# ---------------------------------------------------------------------------
# The relative-move arithmetic
# ---------------------------------------------------------------------------

def test_context_reports_the_etf_move_and_the_excess_over_it():
    snapshot = make_snapshot(gap_on_event_pct=8.0, change_since_event_pct=1.0)
    # ETF: 100 -> 102 on the event day (+2%), then -> 102 (0% since).
    provider = ByTickerProvider({"XLK": _bars_with_event(100.0, 102.0, 102.0)})

    ctx = build_sector_context("Semiconductors", snapshot, provider, EVENT_DAY)

    assert ctx is not None
    assert ctx.sector == "Information Technology"
    assert ctx.raw_sector == "Semiconductors"
    assert ctx.etf_ticker == "XLK"
    assert ctx.etf_gap_on_event_pct == 2.0
    assert ctx.etf_change_since_event_pct == 0.0
    # The number the tribunal actually needs: how much of the 8% was the
    # company rather than its sector.
    assert ctx.excess_gap_on_event_pct == 6.0
    assert ctx.excess_change_since_event_pct == 1.0


def test_a_sector_wide_move_leaves_almost_no_excess():
    snapshot = make_snapshot(gap_on_event_pct=7.0, change_since_event_pct=0.0)
    provider = ByTickerProvider({"XLE": _bars_with_event(100.0, 106.5, 106.5)})

    ctx = build_sector_context("Energy", snapshot, provider, EVENT_DAY)

    assert ctx.etf_gap_on_event_pct == 6.5
    assert ctx.excess_gap_on_event_pct == 0.5


def test_excess_is_null_when_the_candidate_side_is_missing():
    """Null means unverified, never zero (invariant 6). A candidate with no
    measured gap must not be reported as having exactly matched its sector."""
    snapshot = make_snapshot(gap_on_event_pct=None, change_since_event_pct=1.0)
    provider = ByTickerProvider({"XLK": _bars_with_event(100.0, 102.0, 102.0)})

    ctx = build_sector_context("Technology", snapshot, provider, EVENT_DAY)

    assert ctx.etf_gap_on_event_pct == 2.0
    assert ctx.excess_gap_on_event_pct is None
    assert ctx.excess_change_since_event_pct == 1.0


def test_no_context_when_the_industry_has_no_etf():
    provider = ByTickerProvider({})

    ctx = build_sector_context("Blockchain Metaverse Holdings", make_snapshot(),
                               provider, EVENT_DAY)

    assert ctx is None
    assert provider.asked == []          # no pointless fetch


def test_no_context_when_there_is_no_event_date_to_measure_against():
    provider = ByTickerProvider({"XLK": _bars_with_event(100.0, 102.0, 102.0)})

    ctx = build_sector_context("Technology", make_snapshot(), provider, None)

    assert ctx is None
    assert provider.asked == []


def test_a_failing_etf_fetch_drops_the_context_instead_of_the_candidate():
    """Comparison material is a nice-to-have. Losing it must never cost us
    the candidate, so the fetch failure is swallowed here and surfaces as an
    absent field."""
    provider = ByTickerProvider({"XLK": RuntimeError("Yahoo timed out")})

    ctx = build_sector_context("Technology", make_snapshot(), provider, EVENT_DAY)

    assert ctx is None


def test_etf_history_that_never_reaches_the_event_yields_nulls_not_zeros():
    stale = [Bar(day=EVENT_DAY - timedelta(days=n), open=100.0, high=100.0,
                 low=100.0, close=100.0, volume=1e6) for n in (5, 4, 3)]
    provider = ByTickerProvider({"XLK": stale})

    ctx = build_sector_context("Technology", make_snapshot(), provider, EVENT_DAY)

    assert ctx is not None
    assert ctx.etf_ticker == "XLK"
    assert ctx.etf_gap_on_event_pct is None
    assert ctx.excess_gap_on_event_pct is None


# ---------------------------------------------------------------------------
# Wiring: dossier assembly and the session-mode packages
# ---------------------------------------------------------------------------

def _wired_brief():
    catalyst = Catalyst(type=CatalystType.EARNINGS_BEAT_RAISE,
                        description="Q2 beat", event_date=EVENT_DAY)
    provider = ByTickerProvider(
        {"TEST": _bars_with_event(100.0, 110.0, 110.0),
         "XLK": _bars_with_event(100.0, 102.0, 102.0)},
        profile_data={"name": "Test Corp", "sector": "Semiconductors"})
    return build_brief("TEST", catalyst, provider), provider


def test_build_brief_attaches_the_sector_context():
    brief, provider = _wired_brief()

    assert brief.sector_context is not None
    assert brief.sector_context.etf_ticker == "XLK"
    assert brief.sector_context.etf_gap_on_event_pct == 2.0
    assert brief.sector_context.excess_gap_on_event_pct == 8.0
    assert "XLK" in provider.asked


def test_build_brief_survives_a_company_with_no_sector():
    catalyst = Catalyst(type=CatalystType.EARNINGS_BEAT_RAISE,
                        description="Q2 beat", event_date=EVENT_DAY)
    provider = ByTickerProvider({"TEST": _bars_with_event(100.0, 110.0, 110.0)},
                                profile_data={"name": "Test Corp"})

    brief = build_brief("TEST", catalyst, provider)

    assert brief.sector_context is None


@pytest.mark.parametrize("render", [
    lambda b: render_bull_input(b, GateReport()),
    lambda b: render_adversary_input(b, GateReport(), {}),
    lambda b: render_judge_input(b, GateReport(), {}, {}),
])
def test_every_role_sees_the_sector_context_in_its_dossier(render):
    """Session mode hands each role a file built by these renderers
    (casefile.write_package). Material none of them can see is material that
    does not exist."""
    brief, _ = _wired_brief()

    payload = json.loads(render(brief).split("\n\n", 1)[1])

    ctx = payload["dossier"]["sector_context"]
    assert ctx["etf_ticker"] == "XLK"
    assert ctx["excess_gap_on_event_pct"] == 8.0


def test_the_bull_package_written_to_disk_carries_the_sector_context(
        monkeypatch, tmp_path):
    """The end of the wire in session mode: a real file on disk, which is
    all a role subagent ever gets to read."""
    monkeypatch.setenv("HAWKEYE_CASES", str(tmp_path / "cases"))
    brief, _ = _wired_brief()
    case = casefile.open_case(brief, GateReport(), nav=100_000.0)

    package = casefile.write_package(case)

    written = json.loads(
        Path(package["input"]).read_text(encoding="utf-8").split("\n\n", 1)[1])
    assert written["dossier"]["sector_context"]["etf_ticker"] == "XLK"
