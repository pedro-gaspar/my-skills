#!/bin/sh
# Local checks before a commit (Wonka has no hosted CI): the unit tests, then `ruff check` when ruff is installed.
# Run from anywhere: ./wonka/check.sh or (cd wonka && ./check.sh).
set -eu
cd "$(dirname "$0")"

python3 -m unittest discover -q

if command -v ruff >/dev/null 2>&1; then
    ruff check --no-cache .
else
    echo "check.sh: ruff not installed, lint skipped (pipx install ruff, or uv tool install ruff)" >&2
fi
