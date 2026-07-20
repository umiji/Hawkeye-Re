#!/usr/bin/env bash
# CortexLab Dup Guard — PreToolUse hook.
# Blocks Linear issue creation unless a duplicate check was recorded for this
# project within the last 45 minutes (marker written by scripts/mark-checked.sh).
# Escape hatch: DUP_GUARD_DISABLE=1 skips the guard entirely.

set -u

if [ "${DUP_GUARD_DISABLE:-0}" = "1" ]; then
  exit 0
fi

TTL_SECONDS=2700
MARKER_DIR="$HOME/.claude/.cortexlab-dup-guard"

project_root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
key=$(printf '%s' "$project_root" | cksum | awk '{print $1}')
marker="$MARKER_DIR/checked-$key"

if [ -f "$marker" ]; then
  now=$(date +%s)
  mtime=$(stat -c %Y "$marker" 2>/dev/null || stat -f %m "$marker" 2>/dev/null || echo 0)
  age=$((now - mtime))
  if [ "$age" -ge 0 ] && [ "$age" -le "$TTL_SECONDS" ]; then
    exit 0
  fi
fi

plugin_root="${CLAUDE_PLUGIN_ROOT:-<plugin root>}"
cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "CortexLab Dup Guard: no duplicate check has been recorded for this project in the last 45 minutes, so Linear issue creation is blocked. Before registering a backlog item you must: (1) run the duplicate-detector subagent (cortexlab-dup-guard plugin) on the feature being registered — it searches the Linear backlog and GitHub branches/PRs for the same or similar work; (2) show its report (alert level, similar issues with status/assignee, similar branches) to the user; (3) if the user still wants to register, run: \\"$plugin_root/scripts/mark-checked.sh\\" and then retry this Linear tool call. The /cortexlab-dup-guard:dup-check and /cortexlab-dup-guard:backlog-add commands perform this whole flow. Do not run mark-checked.sh without actually performing the check."
  }
}
EOF
exit 0
