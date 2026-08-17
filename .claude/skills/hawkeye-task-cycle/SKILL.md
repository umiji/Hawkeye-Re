---
name: hawkeye-task-cycle
description: Use when a new task/ticket request comes in (even a casual one-liner) and before starting implementation — vet it against docs/task-template.md's 8 fields, ask about missing/ambiguous fields, then register it in docs/task-list-hawkeye-re.csv and docs/tasks/T-XXX.md. Also use while executing a confirmed task (implement/test/commit cycle) or writing its completion report.
---

# Hawkeye task cycle

Governs how a task moves from a raw request to a closed, evidence-backed
record. The single source of truth for progress is split across two
places (see `docs/tasks/README.md` for the full rationale and layout):
`docs/task-list-hawkeye-re.csv` (the index — ID/created/updated/name/
status/dependency only) and `docs/tasks/T-XXX.md` (one file per task,
holding purpose/scope/prohibitions/completion criteria/test plan/stop
conditions/progress/evidence). Work **one task at a time** — don't start
a second task before the current one reaches a terminal status (`完了` /
`保留` / `中止`).

## 1. Intake — before writing any code

Check the request against the 8 fields in `docs/task-template.md` (ID/task
name, purpose, scope, prohibitions, completion criteria, test plan, stop
conditions, dependencies). If any field is missing or ambiguous, ask the
user in a bulleted list — do not guess (see Prohibited, below). This
includes field 8 (dependencies): don't leave it blank or `-` just because
nothing obvious comes to mind — confirm 無し explicitly, or the two-form
`T-001（ブロッカー）` / `T-001（推奨: 理由）` notation if there is one.
Field 5 (completion criteria) must include **at least one command the
user or an operational runbook actually runs, with its expected
output** — criteria built only from tests that call functions directly
can all go green while the path the user actually takes stays broken
(T-005, 2026-08-17: all four criteria tested `rerank_after_guidance()`
directly and never ran `hawkeye case open --from-earnings`, the command
the `/hawkeye-run` runbook uses). If that command can only run against
the live environment, the criteria must say so and name who runs it and
when. Once
confirmed, register the task in both places: append a row to
`docs/task-list-hawkeye-re.csv` (ID, 作成日, 更新日 — both today's date —
タスク名, 状態 `未着手`, 依存タスク in short form) and create
`docs/tasks/T-XXX.md` with sections `## 目的` / `## 変更範囲` /
`## 禁止事項` (fields 2-4) / `## 完了条件` (field 5) / `## テスト方法`
(field 6) / `## 停止条件` (field 7), plus a metadata block carrying 作成日
/ 更新日 / 状態 / 進捗 / 依存 (field 8, full text with reasoning — the CSV
row only gets the short form). Get agreement before branching or
implementing.

For the acceptance-criteria judgment calls themselves (how to phrase a
binary check, when a goal needs a round-trip question before starting),
follow `~/.claude/rules/common/goals.md` — this skill only adds the
Hawkeye-specific artifacts (`docs/task-template.md` /
`docs/task-list-hawkeye-re.csv` / `docs/tasks/`), not a second set of
rules for the same call.

## 2. Per-task cycle

1. Confirm spec — re-read the task's 完了条件 (completion criteria) in
   `docs/tasks/T-XXX.md`.
2. Implement.
3. Run tests.
4. Update `docs/task-list-hawkeye-re.csv` (状態 / 更新日) and
   `docs/tasks/T-XXX.md` (進捗 / 証拠) with the real outcome.
5. Git commit, then open a Draft PR.
6. Report with evidence (§4 below).

## 3. Prohibited in every cycle, unless the user explicitly asked for it

- Refactoring or fixing warnings outside the task's stated scope.
- Changing expected values or specs to make a failing test pass.
- Filling a gap in an unclear spec by guessing — ask instead (§1).
- Reusing a task ID that was already assigned, even for an abandoned task.

## 4. Stop and ask a human before proceeding when

- Two specs contradict each other.
- The change would delete existing data, is otherwise destructive, or
  touches an existing DB migration's effect on existing data.
- The working tree already has unconfirmed diffs at session start (check
  `git status` before touching anything).
- The work would exceed the scope recorded for the task in
  `docs/tasks/T-XXX.md`.

## 5. Completion report — a bare "done" is not acceptable

Every completion report must state:

- The diff (which files changed, and what changed).
- Test results (pass/fail counts and the command that was run).
- The actual output of the user/runbook-facing command(s) named in the
  task's 完了条件 — the test suite alone does not satisfy this. If such
  a command could not be run here (live environment only), it goes under
  "unverified" below with who runs it and when, never silently skipped.
- The commit SHA.
- What remains unverified (e.g., not checked against production/live
  data, no real API credentials available in this environment).

## Commands

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest          # fully offline; ScriptedLLM + StaticProvider
```
