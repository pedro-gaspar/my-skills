#!/bin/sh
# Obsidian -> JSON on demand. The board is the primary tool: JSON is the truth and notes are rendered only by
# `wonka sync-obsidian`, which overwrites them, so a note edit is lost unless imported before the next sync.
# Usage: wonka-import-obsidian.sh [WONKA-00012 ...]   (no ids: every live ticket)
set -eu
exec "$(dirname "$0")/wonka" import-notes "$@"
