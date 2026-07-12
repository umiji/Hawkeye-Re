"""Risk Officer — deterministic sizing with veto power.

The tribunal argues about *whether* to buy; the Risk Officer alone decides
*how much*, and can veto a BUY outright. Its rules are numbers in
HawkeyeConfig, pre-registered and immune to narrative pressure.

Sizing rule: fixed fractional risk. We risk ``risk_pct`` of NAV to the stop,
capped by a maximum position weight. Expected value and reward/risk hurdles
convert the thesis's own scenario table into a go/no-go — if the numbers the
Bull wrote down don't clear the bar, the Bull's words cannot save the trade.
"""
from __future__ import annotations

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import PositionPlan, Scenario


def expected_value_pct(scenarios: list[Scenario], entry_price: float) -> float:
    """Scenario-weighted expected return in percent (probabilities renormalized)."""
    total_p = sum(s.probability for s in scenarios)
    if total_p <= 0 or entry_price <= 0:
        return 0.0
    return sum(
        (s.probability / total_p) * ((s.price_target / entry_price) - 1.0) * 100.0
        for s in scenarios
    )


def build_position_plan(
    nav: float,
    entry_price: float,
    stop_price: float,
    target_price: float,
    scenarios: list[Scenario],
    config: HawkeyeConfig,
    open_position_count: int = 0,
) -> PositionPlan:
    vetoes: list[str] = []

    if entry_price <= 0:
        vetoes.append("invalid entry price")
    if stop_price >= entry_price:
        vetoes.append("stop price is not below entry — no defined risk")
    if target_price <= entry_price:
        vetoes.append("target price is not above entry — no defined reward")

    risk_per_share = max(entry_price - stop_price, 0.0)
    reward_per_share = max(target_price - entry_price, 0.0)
    reward_risk = (reward_per_share / risk_per_share) if risk_per_share > 0 else 0.0
    ev_pct = expected_value_pct(scenarios, entry_price) if entry_price > 0 else 0.0

    stop_distance_pct = (risk_per_share / entry_price * 100.0) if entry_price > 0 else 0.0
    if stop_distance_pct > 20.0:
        vetoes.append(f"stop is {stop_distance_pct:.1f}% away — too wide for a catalyst trade")

    if reward_risk < config.min_reward_risk and risk_per_share > 0:
        vetoes.append(
            f"reward/risk {reward_risk:.2f} below minimum {config.min_reward_risk:.2f}")
    if ev_pct < config.min_expected_value_pct:
        vetoes.append(
            f"expected value {ev_pct:+.1f}% below hurdle +{config.min_expected_value_pct:.1f}%")
    if open_position_count >= config.max_positions:
        vetoes.append(f"portfolio already at max positions ({config.max_positions})")

    shares = 0
    position_value = 0.0
    if not vetoes and risk_per_share > 0:
        risk_budget = nav * config.default_risk_pct / 100.0
        shares = int(risk_budget / risk_per_share)
        position_value = shares * entry_price
        max_value = nav * config.max_position_pct / 100.0
        if position_value > max_value:
            shares = int(max_value / entry_price)
            position_value = shares * entry_price
        if shares <= 0:
            vetoes.append("position rounds to zero shares at this NAV")

    return PositionPlan(
        nav=nav,
        risk_pct=config.default_risk_pct,
        entry_ref_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        shares=shares,
        position_value=round(position_value, 2),
        position_pct_nav=round(position_value / nav * 100.0, 2) if nav > 0 else 0.0,
        reward_risk=round(reward_risk, 2),
        expected_value_pct=round(ev_pct, 2),
        max_holding_days=config.max_holding_days,
        vetoes=vetoes,
    )
