"""Deleting the tribunal's scratch files (docs/MASTER_OVERVIEW.ja.md §5.2(7)).

Each role gets a subfolder holding its instructions, input, schema and reply.
Every one of those is either regenerated deterministically from the case JSON
or copied verbatim into it on submit, and nothing reads the subfolder once
the case is done — but it is ~79% of the bytes on disk.

Order is the whole point: the workspace is what makes a failed ledger write
retryable, so it may only be removed *after* the ledger insert is confirmed
(the same ordering bug fixed in M5 on 2026-07-29).

The case JSON itself stays, but as a debugging convenience rather than an
audit trail (downgraded 2026-08-01). It does hold the LLM's raw reply
before parsing and clamping, which the ledger does not — but no code
compares the two, and the file sits in git-ignored var/ outside the hash
chain, so its loss is undetectable. Claiming audit value for a file with
neither a reader nor tamper-evidence is the worst of both worlds: nobody
dares delete it, and nobody notices when it disappears. If that comparison
ever needs to actually happen, the raw values belong in the ledger.
"""
import pytest

from hawkeye.gates.entry_gates import run_entry_gates
from hawkeye.tribunal import casefile
from tests.conftest import (
    attack_payload,
    make_brief,
    thesis_payload,
    verdict_payload,
)


@pytest.fixture(autouse=True)
def cases_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("HAWKEYE_CASES", str(tmp_path / "cases"))


def open_test_case(config):
    brief = make_brief(price=50.0)
    gates = run_entry_gates(brief.snapshot, brief.catalyst, config)
    return casefile.open_case(brief, gates, nav=100_000)


def drive_to_verdict(case, decision="pass"):
    casefile.write_package(case)
    casefile.submit(case, thesis_payload(50.0))
    casefile.write_package(case)
    casefile.submit(case, attack_payload())
    casefile.write_package(case)
    casefile.submit(case, verdict_payload(decision, 0.62))
    return case


def workspace(case):
    return casefile.cases_dir() / case.id


def test_the_workspace_survives_until_the_ledger_write_is_confirmed(config):
    """An answered-but-unconfirmed case must stay resumable. Deleting here
    would strand work that `submit()` refuses to redo."""
    case = drive_to_verdict(config and open_test_case(config))

    assert workspace(case).exists()
    assert case.recommendation_id is None


def test_the_workspace_is_removed_once_the_ledger_write_is_confirmed(config):
    case = drive_to_verdict(open_test_case(config))
    assert workspace(case).exists()

    casefile.mark_complete(case, "rec_123")

    assert not workspace(case).exists()
    # The raw-reply record stays — it is the only audit trail of what the
    # LLM said before the parsers clamped it.
    assert (casefile.cases_dir() / f"{case.id}.json").exists()
    assert casefile.load_case(case.id).recommendation_id == "rec_123"


def test_sweep_removes_workspaces_of_completed_cases_only(config):
    done = drive_to_verdict(open_test_case(config))
    casefile.save_case(done.model_copy(update={"recommendation_id": "rec_1"}))
    in_progress = open_test_case(config)
    casefile.write_package(in_progress)

    removed = casefile.sweep_role_workspaces()

    assert removed == [done.id]
    assert not workspace(done).exists()
    assert workspace(in_progress).exists()


def test_sweep_is_idempotent_and_reports_nothing_the_second_time(config):
    done = drive_to_verdict(open_test_case(config))
    casefile.mark_complete(done, "rec_1")

    assert casefile.sweep_role_workspaces() == []


def test_completing_a_case_twice_does_not_fail_on_the_missing_workspace(config):
    """Retrying a confirmed ledger write must not turn into a crash just
    because the scratch files are already gone."""
    case = drive_to_verdict(open_test_case(config))
    casefile.mark_complete(case, "rec_1")

    casefile.mark_complete(case, "rec_1")

    assert casefile.load_case(case.id).recommendation_id == "rec_1"


def test_sweep_survives_an_unreadable_case_file(config, capsys):
    """One corrupt file must not stop the rest of the cleanup — and must not
    be swallowed silently either."""
    done = drive_to_verdict(open_test_case(config))
    casefile.save_case(done.model_copy(update={"recommendation_id": "rec_1"}))
    (casefile.cases_dir() / "case_broken.json").write_text("{ not json",
                                                           encoding="utf-8")

    removed = casefile.sweep_role_workspaces()

    assert removed == [done.id]
    assert "case_broken" in capsys.readouterr().err
