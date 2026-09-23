#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
RUFF=".venv/Scripts/ruff"
if [ ! -x "$RUFF" ]; then
    echo "ruff not found at $RUFF — run 'uv sync' first."
    exit 1
fi
echo "Running ruff (F821/F811/F823 fatal symbol checks)..."
if "$RUFF" check app/ scripts/; then
    echo
    echo "[LINT OK] No fatal symbol errors."
else
    echo
    echo "[LINT FAILED] Fix the errors above before committing."
    exit 1
fi
