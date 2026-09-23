#!/bin/bash
# auto_feishu 环境自检与一键配置 (macOS / Linux)
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

echo "==================================================="
echo "       auto_feishu 环境自检与一键配置 (macOS)"
echo "==================================================="
echo ""

# 1. Node.js 20+ 检查
if ! command -v node >/dev/null 2>&1; then
    echo "[ERROR] 未检测到 Node.js，请先安装 Node.js 20+ (可运行: brew install node)。"
    exit 1
fi

NODE_MAJOR=$(node -v | sed 's/v//' | cut -d. -f1)
if [ "$NODE_MAJOR" -lt 20 ]; then
    echo "[ERROR] 当前 Node.js 版本为 $(node -v)，auto_feishu 需要 v20 或更高版本。"
    exit 1
fi
echo "[OK] Node.js $(node -v) 检查通过！"

# 2. 依赖检查与安装
if [ ! -f "node_modules/.bin/tsx" ]; then
    echo "[INFO] npm 依赖缺失，正在安装..."
    npm ci --ignore-scripts 2>/dev/null || npm install --ignore-scripts
fi
echo "[OK] npm 依赖检查通过！"

# 3. Playwright Chromium 安装
echo "[INFO] 正在检查 Playwright Chromium 浏览器驱动..."
if node -e "const { chromium } = require('playwright'); const p = chromium.executablePath(); process.exit(require('fs').existsSync(p) ? 0 : 1);" 2>/dev/null; then
    echo "[OK] Playwright Chromium 浏览器驱动已就绪！"
else
    echo "[INFO] 正在安装 Chromium 浏览器驱动（已启用国内镜像加速）..."
    rm -rf "$HOME/Library/Caches/ms-playwright/__dirlock" "$HOME/.cache/ms-playwright/__dirlock" 2>/dev/null || true
    PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright" npx playwright install chromium || {
        echo "[ERROR] Chromium 安装失败。请手动执行: PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright npx playwright install chromium"
        exit 1
    }
fi

# 4. 运行飞书自动化配置
DEPLOY_MODE="${1:-}"
if [ -z "$DEPLOY_MODE" ]; then
    echo ""
    echo "请选择飞书应用发布模式："
    echo "  1. 个人用（仅创建者可用，不改变可用范围）"
    echo "  2. 公用（可用范围全员，支持群成员一键导入白名单）"
    printf "请选择 [1/2] (默认 1): "
    read -r CHOICE_MODE
    if [ "$CHOICE_MODE" = "2" ]; then
        DEPLOY_MODE="public"
    else
        DEPLOY_MODE="personal"
    fi
fi

echo ""
echo "[INFO] 正在以【${DEPLOY_MODE}】模式启动飞书自动配置流程..."
npx tsx src/feishu-setup.ts --mode "$DEPLOY_MODE"
