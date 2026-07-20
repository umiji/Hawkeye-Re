#!/usr/bin/env bash
# CortexLab Dup Guard — records that a duplicate check was completed for the
# current project. The PreToolUse guard (hooks/dup-guard.sh) reads this marker
# and allows Linear issue creation for TTL_SECONDS after it is touched.
# Keep the key derivation in sync with hooks/dup-guard.sh.

set -eu

MARKER_DIR="$HOME/.claude/.cortexlab-dup-guard"

project_root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
key=$(printf '%s' "$project_root" | cksum | awk '{print $1}')

mkdir -p "$MARKER_DIR"
touch "$MARKER_DIR/checked-$key"
echo "dup-guard: duplicate check recorded for $project_root (valid 45 min)"
