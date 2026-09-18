#!/bin/bash
# 配置 macOS 开机自启（launchd LaunchAgent，当前用户登录后自动拉起服务）。
# 等价于 Windows 的 setup_autostart.bat（Startup 目录快捷方式）。
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/mac_common.sh"


PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/com.mycodex.service.plist"
mkdir -p "$PLIST_DIR"

cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mycodex.service</string>
    <key>ProgramArguments</key>
    <array>
        <string>$DIR/start_mac.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$MAC_LOG_DIR/macos-launchd.log</string>
    <key>StandardErrorPath</key>
    <string>$MAC_LOG_DIR/macos-launchd.log</string>
</dict>
</plist>
EOF

# 若旧条目存在先卸载（幂等重配）
launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl load "$PLIST" 2>/dev/null || echo "[WARN] launchctl load 失败（可注销重登后手动执行同命令）"

echo "[SUCCESS] MyCodex 开机自启已配置"
echo "  plist: $PLIST"
echo "  取消自启: launchctl unload $PLIST && rm $PLIST"
