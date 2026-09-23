"""企业微信智能机器人配置向导。

流程：打印管理后台操作步骤 → 输入 BotID/Secret → 实测长连接订阅连通 →
写入 .env（WECOM_BOT_ID / WECOM_SECRET，可选追加 ALLOWED_USERS）。

注意：实测订阅会短暂挤掉同一机器人的既有长连接（若 MyCodex 已在运行
并启用了企微），服务端有自动重连，属预期行为。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
WS_URL = "wss://openws.work.weixin.qq.com"

STEPS = """
================================================================
 第 1 步  登录企业微信管理后台
          https://work.weixin.qq.com/wework_admin/loginpage_wx
 第 2 步  应用管理 → 管理工具 → 智能机器人 → 创建机器人
          （若没有"创建"入口，需管理员在 管理工具→智能机器人→管理
            →API模式管理 中授予你创建长连接机器人的权限）
 第 3 步  机器人配置页开启「API 模式」并选择「长连接」方式
 第 4 步  记录页面上展示的 BotID 和 Secret
          （注意：这是长连接专用 Secret，与回调模式的
            Token/EncodingAESKey 不是一回事）
================================================================
"""


def upsert_env(key: str, value: str) -> None:
    lines = ENV_PATH.read_text("utf-8").splitlines() if ENV_PATH.exists() else []
    replacement = f"{key}={value}"
    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = replacement
            updated = True
            break
    if not updated:
        lines.append(replacement)
    ENV_PATH.write_text("\n".join(lines) + "\n", "utf-8")


async def test_connection(bot_id: str, secret: str) -> tuple[bool, str]:
    """连接 → 订阅 → ping → 关闭。返回 (是否成功, 摘要)。"""
    from websockets.asyncio.client import connect

    try:
        async with connect(WS_URL, open_timeout=20, ping_interval=None) as ws:
            rid = uuid.uuid4().hex
            await ws.send(json.dumps({
                "cmd": "aibot_subscribe",
                "headers": {"req_id": rid},
                "body": {"bot_id": bot_id, "secret": secret},
            }))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if resp.get("errcode") != 0:
                return False, f"订阅失败 errcode={resp.get('errcode')}: {resp.get('errmsg')}"
            rid2 = uuid.uuid4().hex
            await ws.send(json.dumps({"cmd": "ping", "headers": {"req_id": rid2}}))
            pong = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if pong.get("errcode") != 0:
                return False, f"心跳失败 errcode={pong.get('errcode')}"
            return True, "订阅 + 心跳全部通过"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    print("MyCodex 企业微信配置向导")
    print(STEPS)

    if not ENV_PATH.exists():
        from_env = ""
        print(f"⚠️ 未找到 {ENV_PATH}，将新建（建议先运行 MyCodex-Setup.bat）")
    else:
        from_env = ""
        for line in ENV_PATH.read_text("utf-8").splitlines():
            if line.startswith("WECOM_BOT_ID="):
                from_env = line.split("=", 1)[1].strip()
    if from_env:
        print(f"当前已配置 BotID: {from_env}（直接回车保留）")

    bot_id = input("请粘贴 BotID: ").strip() or from_env
    if not bot_id:
        print("❌ 未输入 BotID，已退出")
        return 1
    secret = input("请粘贴 Secret: ").strip()
    if not secret:
        print("❌ 未输入 Secret，已退出")
        return 1

    print("\n→ 正在实测长连接（订阅 + 心跳）...")
    ok, detail = asyncio.run(test_connection(bot_id, secret))
    if not ok:
        print(f"❌ 连接测试失败：{detail}")
        print("   请核对 BotID/Secret、确认 API 模式已选「长连接」后重试。")
        return 1
    print(f"✅ 连接测试通过：{detail}")
    print("   （若 MyCodex 正在运行并已启用企微，旧连接会被本次测试短暂挤掉，服务会自动重连）")

    upsert_env("WECOM_BOT_ID", bot_id)
    upsert_env("WECOM_SECRET", secret)
    print(f"✅ 已写入 {ENV_PATH}（WECOM_BOT_ID / WECOM_SECRET）")

    userid_input = input("\n可选：输入你的企业微信 userid 加入企微专属白名单（直接输入 userid，多个用逗号隔开，回车跳过表示全员可用）: ").strip()
    if userid_input:
        existing = ""
        if ENV_PATH.exists():
            for line in ENV_PATH.read_text("utf-8").splitlines():
                if line.startswith("WECOM_ALLOWED_USERS="):
                    existing = line.split("=", 1)[1].strip()
        users = [u.strip() for u in existing.split(",") if u.strip()]
        for u in userid_input.split(","):
            u = u.strip()
            if u.startswith("wecom:"):
                u = u[len("wecom:"):].strip()
            if u and u not in users:
                users.append(u)
        upsert_env("WECOM_ALLOWED_USERS", ",".join(users))
        print(f"✅ 已把 {','.join(users)} 写入 WECOM_ALLOWED_USERS")
        print("   提示：首次给机器人发消息后，日志中会打印你的 userid，可据此核对。")

    print("\n全部完成。请运行 MyCodex-Restart.bat 重启服务使企微通道生效，")
    print("然后到企业微信里找到该机器人发一条消息（如 /help）验证。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
