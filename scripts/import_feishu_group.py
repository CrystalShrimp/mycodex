"""飞书特定群聊成员一键导入白名单工具 (MyCodex)。

功能：
1. 自动读取根目录 .env 中的 FEISHU_APP_ID 与 FEISHU_APP_SECRET；
2. 自动获取 tenant_access_token；
3. 智能发现机器人所在的群聊列表，支持序号快捷选择或手动输入群 chat_id (oc_xxx)；
4. 分页拉取该群内全部成员的 open_id (ou_xxx)；
5. 自动保留现有的企业微信白名单，将群成员一键写入 .env 的 ALLOWED_USERS。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
OPEN_API_BASE = "https://open.feishu.cn/open-apis"


def read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def upsert_env(key: str, value: str) -> None:
    lines = ENV_PATH.read_text("utf-8").splitlines() if ENV_PATH.exists() else []
    replacement = f"{key}={value}"
    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}=") or line.strip() == key:
            lines[i] = replacement
            updated = True
            break
    if not updated:
        lines.append(replacement)
    ENV_PATH.write_text("\n".join(lines) + "\n", "utf-8")


def _http_request(url: str, method: str = "GET", headers: dict | None = None, data: dict | None = None) -> dict:
    req_headers = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        req_headers.update(headers)
    body_bytes = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        try:
            return json.loads(err_body)
        except Exception:
            return {"code": e.code, "msg": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def get_tenant_access_token(app_id: str, app_secret: str) -> str:
    url = f"{OPEN_API_BASE}/auth/v3/tenant_access_token/internal"
    res = _http_request(url, method="POST", data={"app_id": app_id, "app_secret": app_secret})
    if res.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败 (code={res.get('code')}): {res.get('msg')}")
    return res["tenant_access_token"]


def list_bot_chats(token: str) -> list[dict]:
    """获取机器人所在的群聊列表。"""
    url = f"{OPEN_API_BASE}/im/v1/chats?page_size=50"
    headers = {"Authorization": f"Bearer {token}"}
    res = _http_request(url, method="GET", headers=headers)
    if res.get("code") != 0:
        return []
    return res.get("data", {}).get("items", []) or []


def fetch_chat_members(token: str, chat_id: str) -> list[str]:
    """分页拉取群内所有成员的 open_id。"""
    headers = {"Authorization": f"Bearer {token}"}
    members: list[str] = []
    page_token = ""
    for _ in range(50):
        url = f"{OPEN_API_BASE}/im/v1/chats/{chat_id}/members?member_id_type=open_id&page_size=100"
        if page_token:
            url += f"&page_token={urllib.parse.quote(page_token)}"
        res = _http_request(url, method="GET", headers=headers)
        code = res.get("code")
        if code != 0:
            msg = res.get("msg", "")
            if code in (230001, 230002):
                raise RuntimeError(
                    f"机器人不在该群聊内 (code={code})。\n"
                    f"💡 解决方法：请先在飞书该群中点击右上角设置，将机器人添加进群后再试！"
                )
            raise RuntimeError(f"拉取群成员失败 (code={code}): {msg}")
        data = res.get("data", {})
        for item in data.get("items", []):
            mid = item.get("member_id")
            if mid and mid not in members:
                members.append(mid)
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
    return members


def main() -> int:
    print("=" * 60)
    print("      飞书群聊成员一键导入白名单工具 (MyCodex)")
    print("=" * 60)

    env_vars = read_env()
    app_id = env_vars.get("FEISHU_APP_ID", "").strip()
    app_secret = env_vars.get("FEISHU_APP_SECRET", "").strip()

    if not app_id or not app_secret:
        print("❌ 未在 .env 中检测到有效的 FEISHU_APP_ID / FEISHU_APP_SECRET。")
        print("   请先运行 auto_feishu/setup.cmd 完成飞书基础自动化配置后再使用本工具。")
        return 1

    print("→ 正在连接飞书开放平台验证凭证...")
    try:
        token = get_tenant_access_token(app_id, app_secret)
    except Exception as e:
        print(f"❌ 获取飞书凭证失败: {e}")
        return 1
    print("✅ 飞书应用鉴权通过！\n")

    target_chat_id = ""
    if len(sys.argv) > 1 and sys.argv[1].strip():
        target_chat_id = sys.argv[1].strip()
        print(f"检测到命令行传入的目标群 ID: {target_chat_id}")
    else:
        print("→ 正在检索机器人所在的群聊列表...")
        chats = list_bot_chats(token)
        if chats:
            print("\n📋 发现机器人已加入以下群聊：")
            for idx, c in enumerate(chats, 1):
                name = c.get("name") or "(未命名群聊)"
                cid = c.get("chat_id", "")
                desc = c.get("description") or ""
                extra = f" - {desc}" if desc else ""
                print(f"  [{idx}] {name} (ID: {cid}){extra}")
            print()
            choice = input(f"请选择群聊序号 [1-{len(chats)}] 或直接粘贴其他群聊的 chat_id (直接回车取消): ").strip()
            if not choice:
                print("已取消操作。")
                return 0
            if choice.isdigit() and 1 <= int(choice) <= len(chats):
                target_chat_id = chats[int(choice) - 1]["chat_id"]
            else:
                target_chat_id = choice
        else:
            print("💡 机器人当前暂未加入任何群聊。")
            print("   说明：在飞书电脑端打开群聊，点击右上角设置/头像，在弹窗最底部可复制“群 ID”（以 oc_ 开头）。")
            print("   ⚠️ 注意：提取成员前，请确保已把机器人添加到该群聊中！\n")
            target_chat_id = input("请输入目标群聊的 chat_id (以 oc_ 开头，直接回车取消): ").strip()
            if not target_chat_id:
                print("已取消操作。")
                return 0

    print(f"\n→ 正在拉取群 [{target_chat_id}] 的所有成员列表...")
    try:
        members = fetch_chat_members(token, target_chat_id)
    except Exception as e:
        print(f"\n❌ {e}")
        return 1

    if not members:
        print("⚠️ 未获取到该群的任何有效成员。")
        return 1

    print(f"\n✅ 成功获取群内共 {len(members)} 名成员的 Open ID！")
    preview_count = min(len(members), 5)
    print(f"   预览前 {preview_count} 名: {', '.join(members[:preview_count])}{'...' if len(members) > preview_count else ''}")

    current_allowed = env_vars.get("ALLOWED_USERS", "")
    existing_list = [u.strip() for u in current_allowed.split(",") if u.strip()]
    wecom_users = [u for u in existing_list if u.startswith("wecom:")]
    feishu_users = [u for u in existing_list if not u.startswith("wecom:")]

    if feishu_users:
        print(f"\n当前 .env 中已有 {len(feishu_users)} 名飞书白名单用户。")
        merge_choice = input("是否保留原有白名单用户？[Y/N] (Y=追加合并，N=以本次群成员覆盖，默认 Y): ").strip().lower()
        if merge_choice != "n":
            for u in feishu_users:
                if u not in members:
                    members.append(u)

    final_users = wecom_users + members
    upsert_env("ALLOWED_USERS", ",".join(final_users))

    print("\n" + "=" * 60)
    print(f"🎉 成功将 {len(members)} 名飞书群成员写入 .env 的 ALLOWED_USERS！")
    if wecom_users:
        print(f"   （同时保留了已有的 {len(wecom_users)} 名企微用户）")
    print("💡 提示：若服务正在运行，请执行 MyCodex-Restart.bat 重启服务使新名单生效。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
