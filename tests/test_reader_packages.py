"""The two reading steps get a real instruction file, and a rejected reply
does not take the material with it (T-015).

Five agent jobs stand between a fresh earnings print and a recommendation:
the company's outlook, why the quarter came out where it did, and the three
tribunal roles. In session mode each is a throwaway subagent, and what tells
it its job is a file the CLI writes. The three tribunal roles have had one
since session mode existed (`hawkeye/tribunal/casefile.py::write_package`).
The two readers never did — their instructions existed only on the API path,
as `CAUSE_SYSTEM` / `GUIDANCE_SYSTEM`, so a session driving them had to
improvise something.

It cost a reading on 2026-08-18: the improvised instruction named the unit
`"pct"`, the gate accepts `"percent"`, and the submit path discarded the
staged summary on its way out. AMBQ could not be re-read for that scan.

So two things are pinned here. The instruction reaching the reader is the
SAME constant the API path sends — not a copy, not a rewrite, since two
engines reading different text produce results that cannot be compared
(`CLAUDE.md` invariant 4). And a reply the gate refuses on a MECHANICAL
ground leaves the case staged, while `explained: false` — the reader
legitimately reporting the release says nothing — still clears it, because
that is a final answer and not a retryable one.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

from hawkeye import cli
from hawkeye.ledger.stocks import StockStore
from hawkeye.marketdata.base import StaticProvider
from hawkeye.scout import cause_case, guidance_case
from hawkeye.scout.cause_agent import CAUSE_SYSTEM
from hawkeye.scout.cause_agent import build_schema as cause_schema
from hawkeye.scout.cause_agent import render_request as render_cause
from hawkeye.scout.guidance_agent import GUIDANCE_SYSTEM
from hawkeye.scout.guidance_agent import build_schema as guidance_schema
from hawkeye.scout.guidance_agent import render_request as render_guidance
from hawkeye.scout.scout import run_scout
from tests.conftest import FakeWhispers, make_bars, make_whispers
from tests.test_scout_quality_wiring import FakeCalendar, _entries

# One summary carrying both readings: a forward range for the guidance step,
# and a stated reason for the cause step. Deliberately the vendor's shape —
# the outlook sentence and the consensus sentence sit next to the one that
# actually explains the quarter, which is the whole reason both gates check
# WHICH sentence a quote came out of.
SUMMARY = (
    "Test Corp reported second quarter earnings of $1.20 per share, which "
    "included a one-time tax benefit of $0.30 per share. "
    "The company said it expects third quarter results to range from a loss "
    "of $1.00 per share to breakeven. The current consensus estimate is "
    "earnings of $0.08 per share for the quarter ending September 30, 2026.")

CAUSE_QUOTE = "included a one-time tax benefit of $0.30 per share"
GUIDANCE_QUOTE = ("third quarter results to range from a loss of $1.00 per "
                  "share to breakeven")


def _config():
    from hawkeye.config import HawkeyeConfig
    return HawkeyeConfig()


def _scan(tmp_path) -> StockStore:
    """One scan with NO reader — i.e. session mode, both queues left staged."""
    today = date.today()
    event_day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    run_scout(FakeCalendar(_entries(event_day)),
              StaticProvider(bars=make_bars(30, start_price=40.0,
                                            volume=2_000_000),
                             profile_data={"market_cap": 5e9}),
              _config(), today=today, stock_store=store,
              numbers_source=FakeWhispers({"AMZN": make_whispers(
                  "AMZN", announced=event_day, summary=SUMMARY)}))
    return store


def _submit_args(case_id: str, path) -> argparse.Namespace:
    return argparse.Namespace(case_id=case_id, file=str(path), reader="test")


def _queue_args(case_id: str | None) -> argparse.Namespace:
    return argparse.Namespace(case_id=case_id)


def _reply_file(tmp_path, name: str, payload: dict):
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# --- the instruction file itself --------------------------------------------

def test_the_cause_package_hands_over_the_api_paths_own_instructions(tmp_path):
    """Not a paraphrase of it and not a session's improvisation — the same
    string the metered API path sends. Two engines reading different text
    cannot be compared, which is what invariant 4 is protecting."""
    _scan(tmp_path)
    case = cause_case.list_cases()[0]

    package = cause_case.write_package(case)

    assert package["role"] == "cause"
    from pathlib import Path
    assert Path(package["system"]).read_text(encoding="utf-8") == CAUSE_SYSTEM
    assert (Path(package["input"]).read_text(encoding="utf-8")
            == render_cause(case.request()))
    assert (json.loads(Path(package["schema"]).read_text(encoding="utf-8"))
            == cause_schema())
    # The reply target is named but not created — a file that exists before
    # the reader answers is a file a later step can mistake for an answer.
    assert not Path(package["output"]).exists()


def test_the_cause_instructions_name_the_units_the_gate_accepts(tmp_path):
    """The 2026-08-18 loss in one assertion: the improvised instruction said
    `pct`, the gate takes `percent`, and nothing the reader was shown named
    the difference."""
    _scan(tmp_path)
    case = cause_case.list_cases()[0]
    from pathlib import Path

    text = Path(cause_case.write_package(case)["system"]).read_text(
        encoding="utf-8")

    for unit in ("per_share", "million", "billion", "percent"):
        assert unit in text


def test_the_guidance_package_hands_over_the_api_paths_own_instructions(
        tmp_path):
    _scan(tmp_path)
    case = guidance_case.list_cases()[0]

    package = guidance_case.write_package(case)

    assert package["role"] == "guidance"
    from pathlib import Path
    assert (Path(package["system"]).read_text(encoding="utf-8")
            == GUIDANCE_SYSTEM)
    assert (Path(package["input"]).read_text(encoding="utf-8")
            == render_guidance(case.request()))
    assert (json.loads(Path(package["schema"]).read_text(encoding="utf-8"))
            == guidance_schema())
    assert not Path(package["output"]).exists()


def test_the_package_never_shows_the_reader_the_figure_it_is_measured_against(
        tmp_path):
    """The information rule both readers work under, restated as a file
    check: the cause reader must not see the surprise it is explaining, and
    neither may see a consensus figure of ours. What the VENDOR wrote in the
    summary is the summary's business and stays verbatim."""
    _scan(tmp_path)
    from pathlib import Path

    cause_input = Path(cause_case.write_package(
        cause_case.list_cases()[0])["input"]).read_text(encoding="utf-8")

    assert "surprise" not in cause_input.lower()
    assert "consensus estimate is" in cause_input   # the vendor's own sentence
    assert "%" not in cause_input.replace("100%", "")


def test_discarding_a_case_takes_its_package_with_it(tmp_path):
    """A staged package outliving its case is an instruction file pointing at
    a reading nobody is waiting for, and the next `queue` would list it."""
    _scan(tmp_path)
    case = cause_case.list_cases()[0]
    package = cause_case.write_package(case)
    from pathlib import Path
    assert Path(package["system"]).exists()

    assert cause_case.discard(case.id) is True

    assert not Path(package["system"]).exists()
    assert not Path(package["system"]).parent.exists()
    assert cause_case.list_cases() == []


def test_a_staged_package_is_not_mistaken_for_another_waiting_case(tmp_path):
    """`list_cases()` globs `cau_*.json` / `gdc_*.json`. A schema file written
    beside the case JSON would match that glob and be counted as a second
    company waiting to be read."""
    _scan(tmp_path)
    cause_case.write_package(cause_case.list_cases()[0])
    guidance_case.write_package(guidance_case.list_cases()[0])

    assert len(cause_case.list_cases()) == 1
    assert len(guidance_case.list_cases()) == 1


# --- the queue command emits it ---------------------------------------------

def test_cause_queue_prints_the_four_paths_the_subagent_needs(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HAWKEYE_VAR", str(tmp_path))
    _scan(tmp_path)
    case = cause_case.list_cases()[0]

    assert cli.cmd_cause_queue(_queue_args(case.id)) == 0

    out = capsys.readouterr().out
    for label in ("system:", "input:", "schema:", "write_reply_to:",
                  "submit_with:"):
        assert label in out
    from pathlib import Path
    system_line = next(ln for ln in out.splitlines()
                       if ln.startswith("system:"))
    assert (Path(system_line.split("system:", 1)[1].strip())
            .read_text(encoding="utf-8") == CAUSE_SYSTEM)


def test_guidance_queue_prints_the_four_paths_the_subagent_needs(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HAWKEYE_VAR", str(tmp_path))
    _scan(tmp_path)
    case = guidance_case.list_cases()[0]

    assert cli.cmd_guidance_queue(_queue_args(case.id)) == 0

    out = capsys.readouterr().out
    for label in ("system:", "input:", "schema:", "write_reply_to:",
                  "submit_with:"):
        assert label in out
    from pathlib import Path
    system_line = next(ln for ln in out.splitlines()
                       if ln.startswith("system:"))
    assert (Path(system_line.split("system:", 1)[1].strip())
            .read_text(encoding="utf-8") == GUIDANCE_SYSTEM)


# --- a refused reply keeps the material -------------------------------------

def test_a_wrong_unit_records_the_reason_and_leaves_the_case_re_submittable(
        tmp_path, monkeypatch):
    """AMBQ, 2026-08-18. The unit was `pct`; the gate takes `percent`. The
    refusal is recorded on the print row exactly as before — what changes is
    that the summary is still there to read again."""
    monkeypatch.setenv("HAWKEYE_VAR", str(tmp_path))
    store = _scan(tmp_path)
    case = cause_case.list_cases()[0]
    reply = _reply_file(tmp_path, "bad_unit.json", {
        "explained": True, "nature": "one_off",
        "magnitude": 0.30, "magnitude_unit": "pct",
        "period": "2026-Q2", "quote": CAUSE_QUOTE})

    rc = cli.cmd_cause_submit(_submit_args(case.id, reply))

    row = store.active_print(case.stock_id, "2026-Q2")
    assert row.cause is None
    assert row.cause_reason == "magnitude_unit_missing"
    assert [c.id for c in cause_case.list_cases()] == [case.id]
    assert rc != 0          # the caller has work left to do, and is told so


def test_the_re_submission_actually_lands(tmp_path, monkeypatch):
    """The point of keeping it. A corrected reply against the SAME case id
    attaches the reading it was refused for."""
    monkeypatch.setenv("HAWKEYE_VAR", str(tmp_path))
    store = _scan(tmp_path)
    case = cause_case.list_cases()[0]
    cli.cmd_cause_submit(_submit_args(case.id, _reply_file(
        tmp_path, "bad.json", {
            "explained": True, "nature": "one_off", "magnitude": 0.30,
            "magnitude_unit": "pct", "period": "2026-Q2",
            "quote": CAUSE_QUOTE})))

    rc = cli.cmd_cause_submit(_submit_args(case.id, _reply_file(
        tmp_path, "good.json", {
            "explained": True, "nature": "one_off", "magnitude": 0.30,
            "magnitude_unit": "per_share", "period": "2026-Q2",
            "quote": CAUSE_QUOTE})))

    assert rc == 0
    row = store.active_print(case.stock_id, "2026-Q2")
    assert row.cause is not None
    assert row.cause.magnitude_unit == "per_share"
    assert row.cause.source_excerpt == CAUSE_QUOTE
    assert cause_case.list_cases() == []


def test_no_reason_in_the_release_still_clears_the_queue(tmp_path, monkeypatch):
    """`explained: false` is the reader doing its job. Keeping the case would
    invite a reworded retry, which the skill forbids for exactly the reason
    it looks tempting: asked differently enough, a reader will find one."""
    monkeypatch.setenv("HAWKEYE_VAR", str(tmp_path))
    store = _scan(tmp_path)
    case = cause_case.list_cases()[0]
    reply = _reply_file(tmp_path, "none.json",
                        {"explained": False, "quote": ""})

    rc = cli.cmd_cause_submit(_submit_args(case.id, reply))

    assert rc == 0
    assert cause_case.list_cases() == []
    row = store.active_print(case.stock_id, "2026-Q2")
    assert row.cause_reason == "no_cause_in_source"
    assert row.cause is None


def test_a_guidance_reply_from_the_wrong_sentence_keeps_its_case(
        tmp_path, monkeypatch):
    """The consensus sentence, quoted as though it were the company's own
    range. Mechanical, ours to fix, and therefore retryable."""
    monkeypatch.setenv("HAWKEYE_VAR", str(tmp_path))
    store = _scan(tmp_path)
    case = guidance_case.list_cases()[0]
    reply = _reply_file(tmp_path, "wrong_sentence.json", {
        "guided": True, "periods": [{
            "period": "2026-Q3", "eps_low": 0.08, "eps_high": 0.08,
            "quote": "current consensus estimate is earnings of "
                     "$0.08 per share"}]})

    rc = cli.cmd_guidance_submit(_submit_args(case.id, reply))

    row = store.active_print(case.stock_id, "2026-Q2")
    assert row.guidance_readings == []
    assert row.guidance_reason == "quoted_the_wrong_sentence"
    assert [c.id for c in guidance_case.list_cases()] == [case.id]
    assert rc != 0


def test_a_company_that_guided_nothing_still_clears_the_queue(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HAWKEYE_VAR", str(tmp_path))
    store = _scan(tmp_path)
    case = guidance_case.list_cases()[0]
    reply = _reply_file(tmp_path, "unguided.json",
                        {"guided": False, "periods": []})

    rc = cli.cmd_guidance_submit(_submit_args(case.id, reply))

    assert rc == 0
    assert guidance_case.list_cases() == []
    row = store.active_print(case.stock_id, "2026-Q2")
    assert row.guidance_readings == []
    assert row.guidance_reason == "no_guidance_in_source"


def test_a_kept_case_still_offers_its_package(tmp_path, monkeypatch, capsys):
    """Keeping the case is only useful if the instructions come back with it —
    the reader that got the unit wrong is exactly the one that needs to read
    them again."""
    monkeypatch.setenv("HAWKEYE_VAR", str(tmp_path))
    _scan(tmp_path)
    case = cause_case.list_cases()[0]
    cli.cmd_cause_submit(_submit_args(case.id, _reply_file(
        tmp_path, "bad.json", {
            "explained": True, "nature": "one_off", "magnitude": 0.30,
            "magnitude_unit": "pct", "period": "2026-Q2",
            "quote": CAUSE_QUOTE})))
    capsys.readouterr()

    assert cli.cmd_cause_queue(_queue_args(case.id)) == 0

    from pathlib import Path
    system_line = next(ln for ln in capsys.readouterr().out.splitlines()
                       if ln.startswith("system:"))
    assert (Path(system_line.split("system:", 1)[1].strip())
            .read_text(encoding="utf-8") == CAUSE_SYSTEM)
