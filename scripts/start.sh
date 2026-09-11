#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "Error: .env file not found. Run MyCodex-Setup.bat (Windows) to generate one,"
    echo "or create it manually with FEISHU_APP_ID / FEISHU_APP_SECRET."
    exit 1
fi

exec uv run python -m app.main
