#!/bin/bash
# MyCodex macOS 一键启动（Finder 双击或终端执行）。
# 等价于 Windows 的 MyCodex.bat：停旧进程 → 后台起服务 → 等健康检查。
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
chmod +x "$DIR"/launcher_macos/*.command "$DIR"/scripts/*.sh "$DIR"/auto_feishu/*.sh 2>/dev/null || true
exec "$DIR/scripts/restart_mac.sh"
