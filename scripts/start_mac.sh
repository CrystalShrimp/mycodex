#!/bin/bash
# 后台启动 MyCodex 服务（不先停旧进程；launchd/自启也用它）。
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/mac_common.sh"

if mac_healthy; then
    echo "[INFO] 服务已在运行，无需重复启动。"
    exit 0
fi
mac_start_service
mac_wait_healthy 60
