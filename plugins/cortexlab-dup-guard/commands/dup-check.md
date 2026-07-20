---
description: これから作る機能・タスクの重複をLinearバックログとGitHubブランチ/PRから事前チェックする
argument-hint: <機能・タスクの説明>
---

You are running CortexLab Dup Guard's pre-development duplicate check.

Target feature/task description:

<task-description>
$ARGUMENTS
</task-description>

## Steps

1. If the description above is empty, ask the user (in Japanese) what feature
   or task they want to check, then continue with their answer.
2. Launch the `duplicate-detector` subagent from this plugin
   (cortexlab-dup-guard) via the Agent tool. Pass it: the task description,
   the current repository's default branch name, and any extra context the
   user gave (component names, related issue IDs). Do NOT investigate
   yourself in this session — the subagent does the searching.
3. Relay the subagent's report to the user in Japanese, keeping its alert
   level (🔴 / 🟡 / 🟢), the Linear and GitHub findings tables, and the
   recommended action. Do not soften or omit findings.
4. Record that a duplicate check was completed, so the plugin's
   Linear-registration guard hook unlocks issue creation for the next
   45 minutes in this project. Run exactly:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/mark-checked.sh"
   ```

   Run this AFTER presenting the report — never before, and never to bypass
   a check that did not actually happen.
5. Close with next-step guidance:
   - 🟢 → そのままLinear登録・着手してよい旨を伝える(希望があれば
     `/cortexlab-dup-guard:backlog-add` で登録できることを案内)。
   - 🟡 → 類似Issueの担当者・ステータスを示し、分担相談または既存Issueへの
     合流を提案する。
   - 🔴 → 新規登録・着手を止めることを明確に推奨し、既存のIssue/ブランチに
     合流する選択肢を示す。
