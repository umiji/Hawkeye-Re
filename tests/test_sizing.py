from hawkeye.contracts.models import Scenario
from hawkeye.risk.sizing import build_position_plan, expected_value_pct


def scenarios(entry=100.0):
    return [
        Scenario(name="bear", probability=0.25, price_target=entry * 0.90),
        Scenario(name="base", probability=0.50, price_target=entry * 1.12),
        Scenario(name="bull", probability=0.25, price_target=entry * 1.25),
    ]


def test_expected_value():
    ev = expected_value_pct(scenarios(), 100.0)
    assert abs(ev - (0.25 * -10 + 0.5 * 12 + 0.25 * 25)) < 1e-9


def test_ev_renormalizes_probabilities():
    scen = [Scenario(name="a", probability=0.5, price_target=110.0),
            Scenario(name="b", probability=0.25, price_target=90.0)]
    ev = expected_value_pct(scen, 100.0)  # weights 2/3, 1/3
    assert abs(ev - (2 / 3 * 10 + 1 / 3 * -10)) < 1e-9


def test_risk_budget_sizing(config):
    plan = build_position_plan(nav=100_000, entry_price=100.0, stop_price=92.0,
                               target_price=118.0, scenarios=scenarios(),
                               config=config)
    assert plan.approved
    # risk budget = 0.75% of 100k = $750; $8 risk/share -> 93 shares (< 10% cap)
    assert plan.shares == 93
    assert plan.reward_risk == 2.25


def test_position_cap_binds_when_stop_tight(config):
    plan = build_position_plan(nav=100_000, entry_price=100.0, stop_price=95.0,
                               target_price=112.0, scenarios=scenarios(),
                               config=config)
    # $5 risk/share -> 150 shares = $15k = 15% NAV; capped to 10% = 100 shares
    assert plan.approved
    assert plan.shares == 100
    assert plan.position_pct_nav == 10.0


def test_position_cap_binds(config):
    # tight stop would imply a huge position; the 10% NAV cap must bind
    plan = build_position_plan(nav=100_000, entry_price=100.0, stop_price=99.5,
                               target_price=101.5, scenarios=scenarios(),
                               config=config)
    assert plan.position_pct_nav <= config.max_position_pct + 1e-6


def test_veto_bad_reward_risk(config):
    plan = build_position_plan(nav=100_000, entry_price=100.0, stop_price=90.0,
                               target_price=105.0, scenarios=scenarios(),
                               config=config)
    assert not plan.approved
    assert any("reward/risk" in v for v in plan.vetoes)


def test_veto_negative_ev(config):
    bad = [Scenario(name="bear", probability=0.6, price_target=85.0),
           Scenario(name="base", probability=0.4, price_target=105.0)]
    plan = build_position_plan(nav=100_000, entry_price=100.0, stop_price=95.0,
                               target_price=112.0, scenarios=bad, config=config)
    assert not plan.approved
    assert any("expected value" in v for v in plan.vetoes)


def test_veto_stop_above_entry(config):
    plan = build_position_plan(nav=100_000, entry_price=100.0, stop_price=105.0,
                               target_price=120.0, scenarios=scenarios(),
                               config=config)
    assert not plan.approved


def test_veto_portfolio_full(config):
    plan = build_position_plan(nav=100_000, entry_price=100.0, stop_price=95.0,
                               target_price=112.0, scenarios=scenarios(),
                               config=config, open_position_count=8)
    assert not plan.approved
    assert any("max positions" in v for v in plan.vetoes)
