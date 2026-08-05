"""Offline market data: it changes the SOURCE of numbers, never the gates.

The risk this path introduces is that a human can type any number and have
it look, in the ledger, exactly like a fetched one. These tests pin the two
properties that contain that risk: absent fields stay unverified (so the
gates still fail closed), and every record built this way carries the input
file's digest.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from hawkeye.marketdata import offline
from hawkeye.marketdata.offline import (
    OfflineDataError,
    load_offline_provider,
    provenance_note,
)


def _write(tmp_path, payload: dict):
    path = tmp_path / "data.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _bar(day: str, close: float = 100.0) -> dict:
    return {"day": day, "open": close, "high": close + 1,
            "low": close - 1, "close": close, "volume": 1_000_000}


def test_loads_bars_oldest_first_regardless_of_input_order(tmp_path):
    path = _write(tmp_path, {"bars": [_bar("2026-08-04", 125.33),
                                      _bar("2026-08-03", 114.53),
                                      _bar("2026-07-31", 108.37)]})
    bars = load_offline_provider(path).daily_history("SPCX")
    assert [b.day for b in bars] == [date(2026, 7, 31), date(2026, 8, 3),
                                     date(2026, 8, 4)]
    assert bars[-1].close == 125.33


def test_profile_next_earnings_date_is_parsed_to_a_date(tmp_path):
    path = _write(tmp_path, {
        "bars": [_bar("2026-08-04")],
        "profile": {"name": "Space Exploration Technologies Corp.",
                    "market_cap": 1.57e12,
                    "next_earnings_date": "2026-11-04"}})
    profile = load_offline_provider(path).profile("SPCX")
    assert profile["next_earnings_date"] == date(2026, 11, 4)
    assert profile["market_cap"] == 1.57e12


def test_absent_profile_fields_stay_missing_rather_than_defaulting(tmp_path):
    """Invariant 6: a number nobody supplied must not materialize here.

    build_snapshot() reads market_cap off the profile; if this file quietly
    invented one, an unverified hard gate would turn into a passing one.
    """
    path = _write(tmp_path, {"bars": [_bar("2026-08-04")]})
    profile = load_offline_provider(path).profile("SPCX")
    assert profile.get("market_cap") is None
    assert profile.get("next_earnings_date") is None


def test_news_is_optional_and_timestamps_are_parsed(tmp_path):
    path = _write(tmp_path, {
        "bars": [_bar("2026-08-04")],
        "news": [{"headline": "Q2 results", "source": "CNBC",
                  "published_at": "2026-08-04T20:05:00+00:00"}]})
    items = load_offline_provider(path).news("SPCX")
    assert items[0].headline == "Q2 results"
    assert items[0].published_at.year == 2026


@pytest.mark.parametrize("payload, message", [
    ({}, "non-empty list"),
    ({"bars": []}, "non-empty list"),
    ({"bars": [{"day": "2026-08-04", "open": 1, "high": 2, "low": 1}]}, "close"),
    ({"bars": [{"day": "nonsense", "open": 1, "high": 2, "low": 1,
                "close": 1, "volume": 1}]}, "day"),
    ({"bars": [{"day": "2026-08-04", "open": 1, "high": 1, "low": 5,
                "close": 1, "volume": 1}]}, "high < low"),
    ({"bars": [_bar("2026-08-04"), _bar("2026-08-04")]}, "duplicate"),
])
def test_malformed_files_raise_an_actionable_error(tmp_path, payload, message):
    path = _write(tmp_path, payload)
    with pytest.raises(OfflineDataError) as exc:
        load_offline_provider(path)
    assert message in str(exc.value)


def test_missing_file_names_the_env_var_that_pointed_at_it(tmp_path):
    with pytest.raises(OfflineDataError) as exc:
        load_offline_provider(tmp_path / "absent.json")
    assert offline.ENV_VAR in str(exc.value)


def test_provenance_note_carries_the_file_digest(tmp_path):
    path = _write(tmp_path, {"bars": [_bar("2026-08-04")]})
    note = provenance_note(path)
    assert "OFFLINE DATA" in note
    assert offline.file_digest(path) in note


def test_digest_changes_when_a_single_number_is_edited(tmp_path):
    """The digest is the only thing making a hand-edit detectable."""
    path = _write(tmp_path, {"bars": [_bar("2026-08-04", 125.33)]})
    before = offline.file_digest(path)
    path.write_text(json.dumps({"bars": [_bar("2026-08-04", 525.33)]}),
                    encoding="utf-8")
    assert offline.file_digest(path) != before


def test_offline_path_is_none_unless_the_env_var_is_set(monkeypatch):
    """Offline mode must never be the default — it has to be asked for."""
    monkeypatch.delenv(offline.ENV_VAR, raising=False)
    assert offline.offline_path() is None
    monkeypatch.setenv(offline.ENV_VAR, "/tmp/x.json")
    assert offline.offline_path() is not None


def test_unverified_hard_gate_still_fails_closed_on_offline_data(tmp_path):
    """The whole point: hand-supplied data does not buy a gate pass.

    A file with bars but no market cap must reach the gates as
    ``unverified``, and a hard gate counts that as a failure (invariant 6).
    """
    from hawkeye.config import HawkeyeConfig
    from hawkeye.contracts.models import Catalyst, CatalystType
    from hawkeye.gates.entry_gates import run_entry_gates
    from hawkeye.marketdata.snapshot import build_brief

    bars = [_bar(d, 120.0) for d in
            [f"2026-07-{n:02d}" for n in range(6, 31)]]
    path = _write(tmp_path, {"bars": bars, "profile": {"name": "X"}})
    catalyst = Catalyst(type=CatalystType.EARNINGS_OVERREACTION,
                        description="d", event_date=date(2026, 7, 29))
    brief = build_brief("SPCX", catalyst, load_offline_provider(path))

    assert brief.snapshot.market_cap is None
    report = run_entry_gates(brief.snapshot, catalyst, HawkeyeConfig())
    cap_gate = next(g for g in report.results if g.name == "min_market_cap")
    assert cap_gate.unverified is True
    assert len(report.hard_failures) >= 1
    assert report.ok is False
