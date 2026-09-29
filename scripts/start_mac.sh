#!/bin/bash
# 后台启动 MyCodex 服务（不先停旧进程；launchd/自启也用它）。
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/mac_common.sh"

if mac_healthy; then
    echo "[INFO] 服务已在运行，无需重复启动。"
else
    mac_start_service
    mac_wait_healthy 60
fi
# 登录/自启场景同样拉起菜单栏（幂等，未装组件时自动降级）
mac_start_menubar
