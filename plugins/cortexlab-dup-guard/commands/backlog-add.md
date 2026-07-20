---
description: 重複チェックを通してからLinearにバックログ(Issue)を登録する
argument-hint: <登録したい機能・タスクの説明>
---

You are running CortexLab Dup Guard's checked backlog registration: verify
there is no duplicate work, then create the Linear issue.

Requested backlog item:

<task-description>
$ARGUMENTS
</task-description>

## Steps

1. If the description above is empty, ask the user (in Japanese) what they
   want to register, then continue with their answer.
2. Launch the `duplicate-detector` subagent from this plugin
   (cortexlab-dup-guard) via the Agent tool with the task description and
   current repository context. Do not skip this even if the user sounds
   confident the feature is new.
3. Present the subagent's report (alert level, findings, recommendation) to
   the user in Japanese.
4. Decide how to proceed:
   - 🟢 重複なし → proceed to step 5 directly.
   - 🟡 / 🔴 → show the conflicting issues/branches with their status and
     assignee, and ASK the user whether to (a) register anyway,
     (b) merge into an existing issue (e.g. add a comment or update scope
     instead), or (c) abort. Only proceed to step 5 for (a). Never register
     over a 🔴 finding without explicit user confirmation.
5. Record the completed check by running exactly:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/mark-checked.sh"
   ```

6. Create the Linear issue using the Linear MCP tools:
   - Title: concise, in the language the team uses for that project
     (mirror the style of existing issues you saw in the check).
   - Description: what/why, acceptance criteria if the user gave any, and a
     「関連」 section linking any 🟡 similar issues found in step 2 so the
     relationship is recorded.
   - Team/project/labels/assignee: ask the user only if Linear requires a
     choice you cannot infer from existing issues.
   If no Linear MCP tools are available, tell the user Linear MCP is not
   connected in this session and show them the ready-to-paste title and
   description instead.
7. Report the created issue (identifier + URL) to the user in Japanese.
