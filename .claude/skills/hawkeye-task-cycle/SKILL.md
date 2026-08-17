---
name: hawkeye-task-cycle
description: Use when a new task/ticket request comes in (even a casual one-liner) and before starting implementation — vet it against docs/task-template.md's 8 fields, ask about missing/ambiguous fields, then register it in docs/task-list-hawkeye-re.md. Also use while executing a confirmed task (implement/test/commit cycle) or writing its completion report.
---

# Hawkeye task cycle

Governs how a task moves from a raw request to a closed, evidence-backed
entry in `docs/task-list-hawkeye-re.md`, which is the single source of truth for
progress. Work **one task at a time** — don't start a second task before
the current one reaches a terminal status (`完了` / `保留` / `中止`).

## 1. Intake — before writing any code

Check the request against the 8 fields in `docs/task-template.md` (ID/task
name, purpose, scope, prohibitions, completion criteria, test plan, stop
conditions, dependencies). If any field is missing or ambiguous, ask the
user in a bulleted list — do not guess (see Prohibited, below). This
includes field 8 (dependencies): don't leave it blank or `-` just because
nothing obvious comes to mind — confirm 無し explicitly, or the two-form
`T-001（ブロッカー）` / `T-001（推奨: 理由）` notation if there is one. Once
confirmed, add the formal entry to `docs/task-list-hawkeye-re.md`: field 1
splits across the ID and タスク名 (task name, ~10-40 chars, states the task
plainly) columns; fields 2-4 (purpose/scope/prohibitions) go into
タスク詳細; field 5 goes into 完了条件; field 8 goes into 依存. Get
agreement before branching or implementing.

For the acceptance-criteria judgment calls themselves (how to phrase a
binary check, when a goal needs a round-trip question before starting),
follow `~/.claude/rules/common/goals.md` — this skill only adds the
Hawkeye-specific artifacts (`docs/task-template.md` / `docs/task-list-hawkeye-re.md`),
not a second set of rules for the same call.

## 2. Per-task cycle

1. Confirm spec — re-read the task's 完了条件 (completion criteria) in
   `docs/task-list-hawkeye-re.md`.
2. Implement.
3. Run tests.
4. Update `docs/task-list-hawkeye-re.md` (状態 / 進捗 / 証拠 columns) with the real
   outcome.
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
  `docs/task-list-hawkeye-re.md`.

## 5. Completion report — a bare "done" is not acceptable

Every completion report must state:

- The diff (which files changed, and what changed).
- Test results (pass/fail counts and the command that was run).
- The commit SHA.
- What remains unverified (e.g., not checked against production/live
  data, no real API credentials available in this environment).

## Commands

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest          # fully offline; ScriptedLLM + StaticProvider
```
