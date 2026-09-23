#!/bin/bash
# macOS 运维壳共用函数（被 start/stop/restart 脚本 source）。
# 约定：脚本位于 <root>/scripts/，项目根为其上一级目录。
set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

MAC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAC_LOG_DIR="$MAC_ROOT/logs"
MAC_ENV_FILE="$MAC_ROOT/.env"
MAC_SERVICE_LOG="$MAC_LOG_DIR/macos-service.log"

mkdir -p "$MAC_LOG_DIR"

mac_env_value() {
    # mac_env_value <KEY> [default] — 从根目录 .env 读一个值
    local key="$1" default="${2:-}"
    local value="$default"
    if [ -f "$MAC_ENV_FILE" ]; then
        local found
        found="$(grep -E "^${key}=" "$MAC_ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '\r')"
        [ -n "$found" ] && value="$found"
    fi
    printf '%s' "$value"
}

mac_service_port() {
    printf '%s' "$(mac_env_value PORT 8090)"
}

mac_python() {
    printf '%s' "$MAC_ROOT/.venv/bin/python"
}

mac_stop_service() {
    # 按项目根路径匹配进程（与 stop_mycodex.ps1 同口径：只杀本项目，
    # 不碰同机其他 MyCodex 系部署），另兜底清理服务端口占用者。
    local me="$$"
    local killed=0
    while IFS= read -r line; do
        local pid="${line%% *}"
        local cmd="${line#* }"
        [ -z "$pid" ] && continue
        [ "$pid" = "$me" ] && continue
        case "$cmd" in
            *"$MAC_ROOT"*)
                kill "$pid" 2>/dev/null && killed=$((killed+1)) && echo "killed $pid ${cmd:0:60}"
                ;;
        esac
    done < <(pgrep -fl "python" 2>/dev/null || true)

    local port
    port="$(mac_service_port)"
    local pids
    pids="$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    for pid in $pids; do
        [ "$pid" = "$me" ] && continue
        kill "$pid" 2>/dev/null && killed=$((killed+1)) && echo "killed $pid holding port $port"
    done
    return 0
}

mac_start_service() {
    local py
    py="$(mac_python)"
    if [ ! -x "$py" ]; then
        echo "[ERROR] 未找到 $py — 请先运行 MyCodex-Setup.command（uv sync 创建虚拟环境）。" >&2
        return 3
    fi
    if [ ! -f "$MAC_ENV_FILE" ]; then
        echo "[ERROR] 缺少 $MAC_ENV_FILE — 请先运行 MyCodex-Setup.command。" >&2
        return 2
    fi
    (cd "$MAC_ROOT" && nohup "$py" -m app.main >>"$MAC_SERVICE_LOG" 2>&1 &)
    echo "[INFO] 后端启动指令已发出（日志: logs/macos-service.log）"
}

mac_healthy() {
    local port
    port="$(mac_service_port)"
    curl -sf -m 2 "http://127.0.0.1:${port}/health" >/dev/null 2>&1
}

mac_wait_healthy() {
    local tries="${1:-60}"
    local i
    for ((i = 1; i <= tries; i++)); do
        if mac_healthy; then
            echo "[OK] 服务已就绪: http://127.0.0.1:$(mac_service_port)/health"
            return 0
        fi
        sleep 1
    done
    echo "[WARN] ${tries}s 内服务未就绪，请查看 logs/macos-service.log 排障。" >&2
    return 1
}
