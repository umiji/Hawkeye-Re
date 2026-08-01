"""strategy/TRIBUNAL_ROLES.ja.md must stay true to prompts.py.

The judgment criteria the tribunal actually runs on live in `prompts.py`,
which nobody browsing a strategy folder will find. So the readable version
is generated from that single source, and these tests fail the moment the
two disagree — a document that quietly drifts from what the system does is
worse than no document, because it is trusted.

The prompts themselves deliberately stay in `prompts.py` rather than moving
to files. Two reasons: a prompt rule and the code enforcing it only mean
something together (JUDGE_SYSTEM's "don't BUY over an unaddressed severe
attack" is mechanically enforced by `_judge_rule_check` — invariant 3), and
API mode and session mode reading the SAME constant is what makes their
results comparable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hawkeye.reports.tribunal_roles import (
    DOC_PATH,
    judge_rule_numbers,
    render_tribunal_roles_ja,
    role_suffix,
)
from hawkeye.tribunal import prompts


def test_the_committed_document_matches_what_the_prompts_say_today():
    """Regenerate and compare. Edit prompts.py without re-running
    `hawkeye docs tribunal-roles --write` and this is what stops you."""
    committed = Path(DOC_PATH)
    assert committed.exists(), (
        f"{DOC_PATH} is missing — run: hawkeye docs tribunal-roles --write")
    assert committed.read_text(encoding="utf-8") == render_tribunal_roles_ja(), (
        f"{DOC_PATH} is stale. Regenerate it with: "
        "hawkeye docs tribunal-roles --write")


def test_every_prompt_is_reproduced_verbatim():
    """A paraphrase would let the document and the system diverge in meaning
    while still passing a looser check.

    The shared preamble is printed once and each role prints its own half,
    so what has to hold is that the two halves still reconstruct the prompt
    exactly — and that both halves are actually in the document.
    """
    doc = render_tribunal_roles_ja()
    assert prompts._SHARED_DOCTRINE.strip() in doc
    for system in (prompts.BULL_SYSTEM, prompts.ADVERSARY_SYSTEM,
                   prompts.JUDGE_SYSTEM):
        suffix = role_suffix(system)
        assert prompts._SHARED_DOCTRINE + suffix == system
        assert suffix.strip() in doc


def test_every_judge_rule_has_a_japanese_gloss():
    """The Judge's numbered rules are the ones that overturn a decision. Add
    one to the prompt without explaining it here and this fails, rather than
    the rule silently binding a reader who never learned it existed."""
    doc = render_tribunal_roles_ja()
    for n in judge_rule_numbers():
        assert f"判断ルール{n}" in doc, (
            f"JUDGE_SYSTEM rule {n} has no Japanese gloss in the generated "
            "document — add one in hawkeye/reports/tribunal_roles.py")


def test_a_new_judge_rule_is_detected_rather_than_ignored(monkeypatch):
    monkeypatch.setattr(
        prompts, "JUDGE_SYSTEM",
        prompts.JUDGE_SYSTEM + "\n7. A brand new rule nobody documented.\n")
    assert 7 in judge_rule_numbers()
    with pytest.raises(Exception):
        render_tribunal_roles_ja()


def test_the_document_says_where_the_source_of_truth_is():
    """Anyone who edits the generated file instead of the prompt needs to be
    told immediately, at the top."""
    doc = render_tribunal_roles_ja()
    head = doc.splitlines()[:12]
    assert any("prompts.py" in line for line in head)
    assert any("自動生成" in line for line in head)
