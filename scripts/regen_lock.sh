#!/usr/bin/env bash
# Regenerate the universal lockfile from requirements.txt.
# Run after editing requirements.txt to bump or add a dep.
set -euo pipefail
cd "$(dirname "$0")/.."
if ! command -v uv >/dev/null 2>&1; then
    echo "uv not in PATH. Install from https://docs.astral.sh/uv/"
    exit 1
fi
uv pip compile --universal --quiet requirements.txt -o requirements.lock
echo "Wrote requirements.lock ($(grep -cE '^[a-z0-9]' requirements.lock) pinned packages)."
