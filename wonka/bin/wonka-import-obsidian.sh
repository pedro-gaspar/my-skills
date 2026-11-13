#!/bin/sh
# Obsidian -> JSON on demand. The board writes JSON and renders notes from it, so note edits are lost until imported.
# Usage: wonka-import-obsidian.sh [WONKA-00012 ...]   (no ids: every live ticket)
set -eu
exec "$(dirname "$0")/wonka" import-notes "$@"
