from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import (
    CandidateBrief,
    Catalyst,
    CatalystType,
    MarketSnapshot,
)
from hawkeye.marketdata.base import Bar
from hawkeye.marketdata.whispers import EASTERN, WhispersRecord


@pytest.fixture
def config() -> HawkeyeConfig:
    return HawkeyeConfig()


def make_whispers(ticker: str = "TEST", announced: date = date(2026, 7, 31),
                  **overrides) -> WhispersRecord:
    """One print as the earnings feed would report it.

    Complete by default — both actuals and both consensus figures present —
    because that is the only shape the funnel accepts as the feed's own
    reading. A test that wants the fallback branch blanks one field.
    """
    base = dict(
        ticker=ticker, name=ticker,
        announced_at=datetime(announced.year, announced.month, announced.day,
                              16, 5, tzinfo=EASTERN),
        quarter_end=date(2026, 6, 30), fiscal_quarter="2026-Q2",
        eps_actual=1.20, eps_consensus=1.00,
        eps_consensus_high=None, eps_consensus_low=None,
        revenue_actual=1.05e9, revenue_consensus=1.00e9, whisper=None)
    base.update(overrides)
    return WhispersRecord(**base)


class FakeWhispers:
    """The earnings feed, stubbed by ticker. A stored exception is raised
    rather than returned, exactly as the live reader does."""

    def __init__(self, by_ticker: dict):
        self.by_ticker = by_ticker
        self.asked: list[str] = []

    def details(self, ticker: str):
        self.asked.append(ticker)
        answer = self.by_ticker.get(ticker)
        if isinstance(answer, Exception):
            raise answer
        return answer


def make_snapshot(**overrides) -> MarketSnapshot:
    base = dict(
        ticker="TEST",
        price=50.0,
        prev_close=49.5,
        market_cap=5e9,
        avg_dollar_volume_20d=50e6,
        atr_pct_14d=3.0,
        gap_on_event_pct=8.0,
        change_since_event_pct=1.0,
        days_since_event=3,
        next_earnings_date=date.today() + timedelta(days=80),
    )
    base.update(overrides)
    return MarketSnapshot(**base)


def make_brief(**snapshot_overrides) -> CandidateBrief:
    return CandidateBrief(
        ticker="TEST",
        company_name="Test Corp",
        snapshot=make_snapshot(**snapshot_overrides),
        catalyst=Catalyst(
            type=CatalystType.EARNINGS_BEAT_RAISE,
            description="Q2 beat with raised FY guidance",
            event_date=date.today() - timedelta(days=4),
        ),
    )


def make_bars(n: int = 300, start_price: float = 40.0,
              daily_move: float = 0.001, volume: float = 1_000_000,
              end: date | None = None) -> list[Bar]:
    """n trading-day bars ending exactly on/at `end` (default today).

    Built backward from `end` so the series always reaches it, however far
    back `n` pushes the start — stepping forward from an estimated start day
    (the previous approach) stops early once weekends are skipped, leaving
    the last bar short of `end` for large `n`.
    """
    end = end or date.today()
    days: list[date] = []
    day = end
    while len(days) < n:
        if day.weekday() < 5:  # trading days only
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    bars = []
    price = start_price
    for day in days:
        close = price * (1 + daily_move)
        bars.append(Bar(day=day, open=price, high=close * 1.01,
                        low=price * 0.99, close=close, volume=volume))
        price = close
    return bars


# --- scripted LLM payloads (schema-shaped dicts) ---------------------------

def thesis_payload(price: float = 50.0) -> dict:
    return {
        "summary": "Beat-and-raise underreaction; drift expected.",
        "edge_type": "underreaction",
        "edge_explanation": "Analysts anchored to old guidance; revisions lag.",
        "other_side": "Momentum-agnostic value screens rotating out on valuation.",
        "claims": [
            {"statement": "At least 80% of covering analysts raise FY EPS estimates",
             "probability": 0.7, "horizon_days": 21,
             "verification": "consensus estimate revision data"},
            {"statement": "Next quarterly guidance is maintained or raised",
             "probability": 0.65, "horizon_days": 90,
             "verification": "next earnings release"},
            {"statement": "No competitor price war announcement in the interim",
             "probability": 0.8, "horizon_days": 45,
             "verification": "sector news scan"},
        ],
        "scenarios": [
            {"name": "bear", "probability": 0.25, "price_target": price * 0.90,
             "rationale": "drift fails, market reverts"},
            {"name": "base", "probability": 0.50, "price_target": price * 1.12,
             "rationale": "standard PEAD magnitude"},
            {"name": "bull", "probability": 0.25, "price_target": price * 1.25,
             "rationale": "estimate revisions compound"},
        ],
        "kill_criteria": [
            {"kind": "price_below", "description": "close below post-event low",
             "level": price * 0.95, "days": None},
            {"kind": "time_stop_days", "description": "drift window expired",
             "level": None, "days": 40},
            {"kind": "event", "description": "guidance cut or CFO departure",
             "level": None, "days": None},
        ],
        "expected_holding_days": 30,
    }


def attack_payload(severe: bool = False) -> dict:
    attacks = [
        {"category": "base_rate", "severity": 3,
         "statement": "Bull case assumes above-median drift magnitude",
         "evidence": "historical PEAD averages are lower", "is_kill_shot": False},
        {"category": "crowding_positioning", "severity": 2,
         "statement": "Post-gap buyers are momentum funds with weak hands",
         "evidence": "", "is_kill_shot": False},
    ]
    if severe:
        attacks.append(
            {"category": "data_integrity", "severity": 5,
             "statement": "The EPS beat is entirely a one-time tax benefit",
             "evidence": "10-Q tax line", "is_kill_shot": True})
    return {"attacks": attacks,
            "strongest_short_case": "Fade the gap: repricing is complete.",
            "summary": "Thesis plausible; magnitude optimistic."}


def verdict_payload(decision: str = "buy", conviction: float = 0.62,
                    addressed: list | None = None) -> dict:
    return {"decision": decision, "conviction": conviction,
            "rationale": "Edge survives attack; sizing per plan.",
            "addressed": addressed or []}
