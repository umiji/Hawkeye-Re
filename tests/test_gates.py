from datetime import date, timedelta

from hawkeye.gates.entry_gates import run_entry_gates
from tests.conftest import make_brief


def test_clean_candidate_passes(config):
    brief = make_brief()
    report = run_entry_gates(brief.snapshot, brief.catalyst, config)
    assert report.ok
    assert not report.hard_failures


def test_low_price_hard_fails(config):
    brief = make_brief(price=3.0)
    report = run_entry_gates(brief.snapshot, brief.catalyst, config)
    assert not report.ok
    assert any(r.name == "min_price" for r in report.hard_failures)


def test_illiquid_hard_fails(config):
    brief = make_brief(avg_dollar_volume_20d=2e6)
    report = run_entry_gates(brief.snapshot, brief.catalyst, config)
    assert not report.ok


def test_stale_event_hard_fails(config):
    brief = make_brief(days_since_event=15)
    report = run_entry_gates(brief.snapshot, brief.catalyst, config)
    assert not report.ok
    assert any(r.name == "catalyst_freshness_days" for r in report.hard_failures)


def test_missing_data_is_unverified_not_pass(config):
    brief = make_brief(market_cap=None)
    report = run_entry_gates(brief.snapshot, brief.catalyst, config)
    assert report.ok  # no hard failure...
    mcap = next(r for r in report.results if r.name == "min_market_cap")
    assert mcap.unverified  # ...but visibly flagged, never silently green


def test_extreme_gap_warns_but_does_not_kill(config):
    brief = make_brief(gap_on_event_pct=35.0)
    report = run_entry_gates(brief.snapshot, brief.catalyst, config)
    assert report.ok
    gap = next(r for r in report.results if r.name == "event_gap_not_extreme")
    assert not gap.passed and not gap.hard


def test_imminent_earnings_warns(config):
    brief = make_brief(next_earnings_date=date.today() + timedelta(days=3))
    report = run_entry_gates(brief.snapshot, brief.catalyst, config)
    earnings = next(r for r in report.results if r.name == "earnings_proximity")
    assert not earnings.passed and not earnings.hard
