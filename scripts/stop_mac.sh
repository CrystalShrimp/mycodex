#!/bin/bash
# 停止本项目的后台服务（backend + 菜单栏）。只按项目根路径匹配，
# 不影响同机其他 MyCodex 系部署。
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=mac_common.sh
. "$DIR/mac_common.sh"

echo "[INFO] 停止 MyCodex 可能存在的旧进程..."
mac_stop_service
echo "[OK] 停止完成。"
