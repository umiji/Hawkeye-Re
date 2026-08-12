"""`hawkeye scout` / `hawkeye rank` as the user actually runs them
(docs/design/RANK_AFTER_GUIDANCE.ja.md).

The defect this pair of commands fixes only shows up at this level: a
candidate's guidance leg is unread at scan time in every mode (nothing inside
a scan process can call the agent that reads it), so the two-command split —
scan now, score once the guidance queue is drained — is what makes the
ledger's recorded shortlist reflect the company's actual outlook instead of
"not yet read" scored as zero.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

from hawkeye import cli
from hawkeye.marketdata.base import StaticProvider
from hawkeye.scout import guidance_case, scan_store
from hawkeye.scout.guidance_agent import parse_reply
from tests.conftest import FakeWhispers, make_bars, make_whispers


class _FakeFinnhub:
    available = True

    def __init__(self, entries):
        self._entries = entries

    def earnings_calendar(self, start, end):
        return self._entries

    def earnings_history(self, ticker, limit=4):
        return []


def _entries(event_day: date, ticker: str, eps_actual: float) -> dict:
    return {"symbol": ticker, "date": event_day.isoformat(),
            "year": 2026, "quarter": 2,
            "epsActual": eps_actual, "epsEstimate": 1.00}


_GUIDED_DOWN_SUMMARY = (
    "The company said it expects third quarter results to range from a "
    "loss of $1.00 per share to breakeven. The current consensus estimate "
    "is earnings of $2.00 per share for the quarter ending September 30, "
    "2026.")


def _wire(monkeypatch, tmp_path, entries, whispers):
    monkeypatch.setenv("HAWKEYE_VAR", str(tmp_path))
    provider = StaticProvider(bars=make_bars(30, start_price=40.0,
                                             volume=2_000_000),
                              profile_data={"market_cap": 5e9})
    monkeypatch.setattr(cli, "FinnhubProvider",
                        lambda *a, **k: _FakeFinnhub(entries))
    monkeypatch.setattr(cli, "CompositeProvider", lambda *a, **k: provider)
    monkeypatch.setattr(cli, "WhispersSource", lambda *a, **k: whispers)


def _scout_args(**overrides):
    base = dict(days=None, monitor_csv=None)
    base.update(overrides)
    return argparse.Namespace(**base)


def _rank_args(**overrides):
    base = dict(evaluate=0, open_cases=0, nav=100_000.0)
    base.update(overrides)
    return argparse.Namespace(**base)


def test_scout_stages_a_scan_and_writes_nothing_to_the_ledger(
        tmp_path, monkeypatch):
    today = date.today()
    event_day = today - timedelta(days=3)
    entries = [_entries(event_day, "AAA", 1.10)]
    _wire(monkeypatch, tmp_path, entries, FakeWhispers({}))

    rc = cli.cmd_scout(_scout_args())

    assert rc == 0
    assert scan_store.has_pending_scan()
    assert cli._ledger().last_scan_at() is None


def test_scout_refuses_to_run_over_an_unranked_scan(tmp_path, monkeypatch):
    today = date.today()
    event_day = today - timedelta(days=3)
    entries = [_entries(event_day, "AAA", 1.10)]
    _wire(monkeypatch, tmp_path, entries, FakeWhispers({}))
    assert cli.cmd_scout(_scout_args()) == 0

    rc = cli.cmd_scout(_scout_args())

    assert rc == 1


def test_rank_refuses_when_nothing_is_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("HAWKEYE_VAR", str(tmp_path))

    assert cli.cmd_rank(_rank_args()) == 1


def test_rank_records_the_scan_and_clears_the_pending_file(
        tmp_path, monkeypatch):
    today = date.today()
    event_day = today - timedelta(days=3)
    entries = [_entries(event_day, "AAA", 1.10)]
    _wire(monkeypatch, tmp_path, entries, FakeWhispers({}))
    cli.cmd_scout(_scout_args())

    rc = cli.cmd_rank(_rank_args())

    assert rc == 0
    assert not scan_store.has_pending_scan()
    assert cli._ledger().last_scan_at() is not None


def test_ranking_after_the_guidance_queue_scores_the_real_reading(
        tmp_path, monkeypatch):
    """The scenario RANK_AFTER_GUIDANCE.ja.md was written for: nothing inside
    a scan process can call the agent that reads guidance, so every scan
    scores that leg as "not yet read" (0 points) and `hawkeye scout` would
    have recorded the shortlist on that alone. `hawkeye rank` is what picks
    up whatever `hawkeye guidance submit` attached in between and folds the
    REAL leg into the score the ledger ends up holding."""
    today = date.today()
    event_day = today - timedelta(days=3)
    entries = [_entries(event_day, "AAA", 1.30), _entries(event_day, "BBB", 1.15)]
    whispers = FakeWhispers({"BBB": make_whispers(
        "BBB", announced=event_day, summary=_GUIDED_DOWN_SUMMARY)})
    _wire(monkeypatch, tmp_path, entries, whispers)

    cli.cmd_scout(_scout_args())
    staged = next(c for c in scan_store.load_scan_result().passed
                  if c.ticker == "BBB")
    assert staged.quality.guidance.status.value == "absent"
    assert staged.quality.breakdown.guidance == 0    # unread, not "guided nothing"

    cases = guidance_case.list_cases()
    assert len(cases) == 1 and cases[0].ticker == "BBB"
    store = cli._stock_store()
    extraction = parse_reply(
        {"guided": True, "period": "2026-Q3", "eps_low": -1.00, "eps_high": 0.0,
         "quote": ("third quarter results to range from a loss of $1.00 "
                   "per share to breakeven")},
        cases[0].request(), model="test-model")
    guidance_case.attach(store, cases[0], extraction)
    guidance_case.discard(cases[0].id)

    cli.cmd_rank(_rank_args())

    assert cli._ledger().last_scan_at() is not None
    bbb_row = next(r for r in cli._ledger().screened_candidates()
                   if r.ticker == "BBB")
    # A real MISS (guided below the $2.00 consensus), not the still-zero
    # "unread" score `hawkeye scout` alone would have committed.
    assert bbb_row.score_breakdown is not None
    assert bbb_row.score_breakdown.guidance < 0
