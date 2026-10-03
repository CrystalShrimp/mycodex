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

注意：企微机器人为单连接（新订阅会踢掉旧连接）。若本地网关服务正在运行，本脚本会
先暂停服务以独占连接，绑定完成后自动恢复（恢复重启同时让新白名单生效）。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
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


def is_local_service_running() -> bool:
    """探测本地网关服务是否在运行（显式禁用代理，避免健康检查被代理劫持误判）。"""
    raw = get_env_value("PORT").strip()
    try:
        port = int(raw) if raw else 8080
    except ValueError:
        port = 8080
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def stop_local_service() -> None:
    """暂停本地网关服务以独占企微机器人长连接（win: stop_*.ps1 / mac: stop_mac.sh）。"""
    if sys.platform == "win32":
        candidates = sorted((ROOT_DIR / "scripts").glob("stop_*.ps1"))
        if not candidates:
            raise RuntimeError("未找到 scripts/stop_*.ps1")
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(candidates[0]), "-CallerPid", str(os.getpid())],
            capture_output=True, text=True,
        )
    else:
        candidates = sorted((ROOT_DIR / "scripts").glob("stop_mac*.sh"))
        if not candidates:
            raise RuntimeError("未找到 scripts/stop_mac.sh")
        subprocess.run(["bash", str(candidates[0])], capture_output=True, text=True)
    time.sleep(1)


def start_local_service() -> bool:
    """恢复本地网关服务并等待健康检查通过；返回是否成功。"""
    if sys.platform != "win32":
        candidates = sorted((ROOT_DIR / "scripts").glob("restart_mac*.sh"))
        if not candidates:
            print("[!] 未找到 scripts/restart_mac.sh，请手动重启服务。")
            return False
        # restart_mac.sh 自带停旧→起新→等健康检查（含 60s 等待），阻塞执行完成后即在线
        subprocess.run(["bash", str(candidates[0])], cwd=str(ROOT_DIR), capture_output=True, text=True)
        print("[*] 正在恢复本地服务...")
        for _ in range(20):
            time.sleep(1)
            if is_local_service_running():
                print("[OK] 本地服务已恢复在线（新白名单已随之生效）。")
                return True
        print("[!] 服务未恢复在线，请手动运行 scripts/restart_mac.sh。")
        return False
    mains = [b for b in sorted((ROOT_DIR / "launcher_windows").glob("*.bat"))
             if not b.name.endswith("-Setup.bat") and not b.name.endswith("-Restart.bat")]
    if not mains:
        print("[!] 未找到主启动脚本 launcher_windows/*.bat，请手动启动服务。")
        return False
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         f"Start-Process -FilePath '{mains[0]}' -WorkingDirectory '{ROOT_DIR}'"],
        capture_output=True, text=True,
    )
    print("[*] 正在恢复本地服务...")
    for _ in range(30):
        time.sleep(1)
        if is_local_service_running():
            print("[OK] 本地服务已恢复在线（新白名单已随之生效）。")
            return True
    print("[!] 服务未在 30 秒内恢复在线，请手动运行 launcher_windows 下的主启动脚本。")
    return False


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
            paused_service = False
            try:
                if is_local_service_running():
                    print("\n[*] 检测到本地服务正在占用企微机器人长连接（企微单连接，互踢会导致监听失聪），")
                    print("    已暂停服务以独占连接，绑定完成后自动恢复（恢复重启同时让新白名单生效）。")
                    stop_local_service()
                    paused_service = True
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
            finally:
                if paused_service:
                    try:
                        start_local_service()
                    except Exception as exc:
                        print(f"[!] 服务自动恢复异常（{exc}），请手动运行 launcher_windows 主启动脚本。")
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
