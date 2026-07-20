---
description: Linearバックログから開発着手する前に、類似Issueの状況・担当者と既存ブランチの実装をチェックする
argument-hint: <LinearのIssue ID (例 CTX-42) または着手したいタスクの説明>
---

You are running CortexLab Dup Guard's pre-implementation check: before the
user starts coding a backlog item, verify nobody else is already building it
and no existing branch already contains it.

Target:

<task>
$ARGUMENTS
</task>

## Steps

1. If the target above is empty, ask the user (in Japanese) which Linear
   issue or task they are about to start.
2. If the target looks like a Linear issue ID, fetch that issue with the
   Linear MCP tools first (title, description, status, assignee) so the
   duplicate search runs on its real content, not just the ID.
3. Launch the `duplicate-detector` subagent from this plugin
   (cortexlab-dup-guard) via the Agent tool. Pass it the issue content (or
   task description) and current repository context. Ask it to pay special
   attention to:
   - similar Linear issues that are **In Progress / In Review** and their
     **assignees** — someone may already be building this;
   - existing branches/PRs that already implement part or all of the feature.
4. Report to the user in Japanese:
   - the alert level and findings tables from the subagent;
   - 着手判断: 「着手してOK」/「◯◯さんが類似Issue(ID)を進行中 — 着手前に
     分担を相談」/「既存ブランチ◯◯に実装済みの可能性 — まず中身を確認」の
     いずれかを明確に。
5. If the check is clear (or the user decides to proceed) and the target is a
   Linear issue, offer to update it via Linear MCP: assign it to the user and
   move it to In Progress, so the other member can see the work is taken.
   Only do this after the user agrees. Also suggest a branch name that embeds
   the issue ID (e.g. `feat/CTX-42-short-slug`) so future duplicate checks
   can match branches to issues.
