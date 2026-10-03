#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
企业微信特定群授权向导（方案 A：长连接实时感应绑定群 chatid + 群成员自动收录）。

工作原理：
1. 读取 .env 中的 WECOM_BOT_ID 和 WECOM_SECRET 建立企业微信智能机器人长连接；
2. 用户在目标企业微信群中将机器人拉入并 @机器人 发送任意一条消息（如“绑定”）；
3. 脚本实时捕获该群的 chatid 与发言人的 userid，并在群内自动回复绑定成功确认；
4. 将捕获到的群 chatid 写入 .env 的 WECOM_ALLOWED_CHATS，并将操作者本人写入 WECOM_ALLOWED_USERS；
5. 服务运行时，凡在该授权群内发言的成员均直接放行，且自动收录其 userid 以开放单聊权限。
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

# 确保 Windows 下控制台 Unicode 安全输出
if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"
WS_URL = "wss://openws.work.weixin.qq.com"


def get_env_value(key: str) -> str:
    if not ENV_PATH.exists():
        return ""
    try:
        for line in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            if k.strip() == key:
                return v.strip().strip("'\"")
    except Exception:
        pass
    return ""


def upsert_env(key: str, value: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines() if ENV_PATH.exists() else []
    replacement = f"{key}={value}"
    updated = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            if k.strip() == key:
                lines[i] = replacement
                updated = True
                break
    if not updated:
        lines.append(replacement)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def listen_for_group_message(bot_id: str, secret: str, timeout_sec: int = 180) -> tuple[str, str]:
    """监听一次群聊 @机器人 消息，返回 (chatid, sender_userid)。"""
    from websockets.asyncio.client import connect

    async with connect(WS_URL, open_timeout=20, ping_interval=None) as ws:
        sub_req = uuid.uuid4().hex
        await ws.send(json.dumps({
            "cmd": "aibot_subscribe",
            "headers": {"req_id": sub_req},
            "body": {"bot_id": bot_id, "secret": secret},
        }))
        sub_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
        if sub_resp.get("errcode") != 0:
            raise RuntimeError(f"长连接订阅失败 errcode={sub_resp.get('errcode')}: {sub_resp.get('errmsg')}")

        print(f"[*] 长连接已就绪！请在 {timeout_sec} 秒内：")
        print("    1. 将该智能机器人添加到目标企业微信群（注：机器人「使用方式」需为「多人使用」才可拉入群聊）")
        print("    2. 在该群内 @机器人 发送任意一条文本消息（例如：@机器人 绑定）")
        print("    （如需取消或改为手动输入 chatid，可随时按 Ctrl+C）\n")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec
        last_ping = loop.time()

        while loop.time() < deadline:
            now = loop.time()
            if now - last_ping >= 25:
                await ws.send(json.dumps({"cmd": "ping", "headers": {"req_id": uuid.uuid4().hex}}))
                last_ping = now

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                continue

            frame = json.loads(raw)
            cmd = frame.get("cmd", "")
            req_id = (frame.get("headers") or {}).get("req_id", "")
            body = frame.get("body") or {}

            if cmd == "aibot_msg_callback":
                chattype = body.get("chattype") or ""
                chatid = (body.get("chatid") or "").strip()
                userid = ((body.get("from") or {}).get("userid") or "").strip()
                if chattype == "group" and chatid:
                    # 在群内自动回复确认
                    if req_id:
                        try:
                            await ws.send(json.dumps({
                                "cmd": "aibot_respond_msg",
                                "headers": {"req_id": req_id},
                                "body": {
                                    "msgtype": "markdown",
                                    "markdown": {
                                        "content": (
                                            f"✅ **已成功授权本群！**\n"
                                            f"- 群聊 ID (`chatid`): `{chatid}`\n"
                                            f"- 操作人 (`userid`): `{userid}`\n\n"
                                            "本群成员现在可在群内 `@机器人` 使用，发言后也会自动开通私聊权限。"
                                        )
                                    },
                                },
                            }))
                        except Exception:
                            pass
                    return chatid, userid
                elif chattype == "single" and userid:
                    print(f"[!] 收到来自 {userid} 的【单聊】消息，但当前需要绑定【群聊】，请在目标企业微信群内 @机器人 发送消息...")

        raise TimeoutError(f"等待群聊消息超时（{timeout_sec} 秒）。")


def main() -> int:
    print("\n" + "=" * 60)
    print("       企业微信特定群授权向导（方案 A：特定群白名单）")
    print("=" * 60)

    bot_id = get_env_value("WECOM_BOT_ID")
    secret = get_env_value("WECOM_SECRET")
    if not bot_id or not secret:
        print("[!] 未在 .env 中检测到 WECOM_BOT_ID / WECOM_SECRET。")
        print("    请先在配置中心选择「5. 配置企业微信 - 公用：全部成员可用」完成机器人配置。")
        return 1

    existing_chats = [c.strip() for c in get_env_value("WECOM_ALLOWED_CHATS").split(",") if c.strip()]
    existing_users = [u.strip() for u in get_env_value("WECOM_ALLOWED_USERS").split(",") if u.strip()]
    if existing_chats:
        print(f"  当前已授权群聊 (WECOM_ALLOWED_CHATS): {', '.join(existing_chats)}")
        mode_ans = input("[?] 是否保留已有授权群并追加新群？[Y/n] (输入 n 则清空重新绑定，直接回车 = 追加): ").strip().lower()
        if mode_ans == "n":
            existing_chats = []

    while True:
        print("\n请选择绑定方式：")
        print("  1. 实时感应绑定（推荐：在目标企微群内 @机器人 发一条消息自动识别 chatid）")
        print("  2. 手动输入群聊 chatid")
        print("  0. 完成并保存退出")
        choice = input("请选择 [1/2/0] (直接回车 = 1): ").strip() or "1"

        if choice == "0":
            break
        elif choice == "2":
            manual = input("请输入目标企微群的 chatid（多个用英文逗号隔开）: ").strip()
            for c in manual.split(","):
                c = c.strip()
                if c and c not in existing_chats:
                    existing_chats.append(c)
                    print(f"[OK] 已添加群聊 chatid: {c}")
        elif choice == "1":
            try:
                chatid, userid = asyncio.run(listen_for_group_message(bot_id, secret))
                if chatid not in existing_chats:
                    existing_chats.append(chatid)
                if userid and userid not in existing_users:
                    existing_users.append(userid)
                print(f"\n[OK] 捕获成功！")
                print(f"     群聊 chatid : {chatid}")
                print(f"     触发人 userid: {userid}")
            except KeyboardInterrupt:
                print("\n[*] 已中断实时监听。")
                manual = input("如需手动输入群聊 chatid 请直接粘贴（直接回车跳过）: ").strip()
                if manual:
                    for c in manual.split(","):
                        c = c.strip()
                        if c and c not in existing_chats:
                            existing_chats.append(c)
            except Exception as e:
                print(f"\n[!] 实时监听未能捕获群聊: {e}")
        else:
            print("[!] 无效选项。")
            continue

        more = input("\n[?] 是否继续绑定其他企业微信群？[y/N] (直接回车 = 结束并保存): ").strip().lower()
        if more != "y":
            break

    if not existing_chats:
        print("\n[!] 未添加任何授权群聊，未改动 WECOM_ALLOWED_CHATS。")
        return 0

    upsert_env("WECOM_ALLOWED_CHATS", ",".join(existing_chats))
    if existing_users:
        upsert_env("WECOM_ALLOWED_USERS", ",".join(existing_users))
    print("\n" + "=" * 60)
    print(f"[OK] 已写入特定群白名单 WECOM_ALLOWED_CHATS={','.join(existing_chats)}")
    if existing_users:
        print(f"[OK] 已同步写入初始成员 WECOM_ALLOWED_USERS={','.join(existing_users)}")
    print("     后续凡在授权群内 @机器人 的成员均直接放行，并会自动收录进单聊白名单。")
    print("     若服务正在运行，请重启服务使最新配置生效。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
