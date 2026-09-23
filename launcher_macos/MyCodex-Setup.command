#!/bin/bash
# MyCodex macOS Setup Launcher（逐行对应 launcher_windows/MyCodex-Setup.bat，双击即可运行）。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 权限自愈：确保项目内脚本具备可执行权限
chmod +x "$ROOT"/launcher_macos/*.command "$ROOT"/scripts/*.sh "$ROOT"/auto_feishu/*.sh 2>/dev/null || true

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

echo "=================================================="
echo "            MyCodex macOS Setup Launcher"
echo "=================================================="
echo ""

# 1. Check and install uv if missing
if ! command -v uv >/dev/null 2>&1; then
    echo "[!] uv not found. Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null 2>&1 || {
        echo "[ERROR] Failed to install uv automatically."
        echo "Please install uv manually from https://docs.astral.sh/uv/"
        read -r -p "Press Enter to exit..." _
        exit 1
    }
fi

# 2. Ensure Python virtual environment (.venv)
if [ ! -x ".venv/bin/python" ]; then
    echo "[*] Initializing Python virtual environment via uv sync..."
    uv sync || {
        echo "[ERROR] uv sync failed. Please check your internet connection."
        read -r -p "Press Enter to exit..." _
        exit 1
    }
fi

# 3. Run cross-platform setup wizard
".venv/bin/python" scripts/setup_wizard.py
if [ $? -ne 0 ]; then
    echo ""
    echo "[!] Setup wizard exited with error."
    read -r -p "Press Enter to exit..." _
    exit 1
fi
