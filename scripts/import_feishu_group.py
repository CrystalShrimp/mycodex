"""飞书特定群聊成员一键导入白名单工具 (MyCodex)。

功能：
1. 自动读取根目录 .env 中的 FEISHU_APP_ID 与 FEISHU_APP_SECRET；
2. 自动获取 tenant_access_token；
3. 智能发现机器人所在的群聊列表，支持序号快捷选择或手动输入群 chat_id (oc_xxx)；
4. 分页拉取该群内全部成员的 open_id (ou_xxx)；
5. 自动保留现有的企业微信白名单，将群成员一键写入 .env 的 ALLOWED_USERS；
6. 记录各群导入成员快照，支持对已导入群聊同步成员变动（新成员加入白名单、退群成员移出）。
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
SNAPSHOT_PATH = ROOT / "config" / "feishu_group_imports.json"
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


def load_snapshots() -> dict[str, list[str]]:
    """读取各群最近一次导入的成员快照 {chat_id: [open_id, ...]}。"""
    try:
        data = json.loads(SNAPSHOT_PATH.read_text("utf-8"))
        return {str(k): [str(x) for x in v] for k, v in data.items() if isinstance(v, list)}
    except Exception:
        return {}


def save_snapshots(snapshots: dict[str, list[str]]) -> None:
    try:
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(json.dumps(snapshots, ensure_ascii=False, indent=2), "utf-8")
    except Exception as e:
        print(f"⚠️ 群成员快照写入失败（不影响白名单写入）: {e}")


def compute_sync_changes(allowed_feishu: list[str], snapshot: list[str], members: list[str]) -> tuple[list[str], list[str]]:
    """计算群成员同步所需的白名单增删，返回 (需新增, 需移除)。

    新增 = 当前群成员里还不在白名单中的（含新入群与曾被清空的）；
    移除 = 上次导入快照中已退群、且当前仍在白名单里的。
    手工添加的非本群 ID 不受影响。
    """
    allowed_set = set(allowed_feishu)
    member_set = set(members)
    to_add = [m for m in members if m not in allowed_set]
    to_remove = [u for u in snapshot if u not in member_set and u in allowed_set]
    return to_add, to_remove


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
            if code in (230001, 230002, 232011):
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
    # Windows 控制台/管道为 GBK 时，✅💡 等 emoji 会触发 UnicodeEncodeError，
    # 统一切到 UTF-8（不可重配置时降级为替换字符，绝不因打印崩溃）
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

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

    # 读取现有白名单
    current_allowed = env_vars.get("ALLOWED_USERS", "")
    existing_list = [u.strip() for u in current_allowed.split(",") if u.strip()]
    wecom_users = [u[len("wecom:"):].strip() for u in existing_list if u.startswith("wecom:")]
    feishu_users = [u for u in existing_list if not u.startswith("wecom:")]

    # 自动迁移残留的企微白名单至 WECOM_ALLOWED_USERS
    if wecom_users:
        existing_wecom = env_vars.get("WECOM_ALLOWED_USERS", "")
        wecom_list = [u.strip() for u in existing_wecom.split(",") if u.strip()]
        for u in wecom_users:
            if u not in wecom_list:
                wecom_list.append(u)
        upsert_env("WECOM_ALLOWED_USERS", ",".join(wecom_list))
        print(f"ℹ️ 检测到历史残留的企微白名单，已自动平滑迁移至 WECOM_ALLOWED_USERS ({len(wecom_list)} 人)")

    # 选择操作分支：重置 / 追加 / 同步已有群的成员变动
    snapshots = load_snapshots()
    print(f"当前已配置飞书白名单人数: {len(feishu_users)} 人")
    print("请选择操作模式：")
    print("  [1] 重置群成员名单（清空旧名单，仅保留本次所选群成员）")
    print("  [2] 添加新的群成员名单（在现有白名单基础上，追加合并新群成员）")
    print("  [3] 同步已导入群聊的成员变动（新入群成员加入白名单，退群成员移出）")
    print("  [0] 取消并返回")
    mode_choice = input("请选择 [1/2/3/0] (直接回车默认 1): ").strip()
    if mode_choice == "0":
        print("已取消操作。")
        return 0
    is_append_mode = mode_choice == "2"
    is_sync_mode = mode_choice == "3"
    mode_desc = (
        "同步已导入群聊的成员变动"
        if is_sync_mode
        else "添加新的群成员名单（追加合并）" if is_append_mode else "重置群成员名单（覆盖原有）"
    )
    print(f"→ 已选定模式：{mode_desc}")
    if is_sync_mode and not snapshots:
        print("ℹ️ 尚无已导入群聊的记录，本次同步等同首次导入（该群全部成员加入白名单）。")
    print()

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
                mark = " ★已导入" if cid in snapshots else ""
                print(f"  [{idx}] {name} (ID: {cid}){extra}{mark}")
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

    final_members: list[str] = []
    sync_added: list[str] = []
    sync_removed: list[str] = []
    if is_append_mode:
        for u in feishu_users:
            if u not in final_members:
                final_members.append(u)
        for m in members:
            if m not in final_members:
                final_members.append(m)
    elif is_sync_mode:
        snapshot = snapshots.get(target_chat_id, [])
        sync_added, sync_removed = compute_sync_changes(feishu_users, snapshot, members)
        removed_set = set(sync_removed)
        final_members = [u for u in feishu_users if u not in removed_set]
        for m in sync_added:
            if m not in final_members:
                final_members.append(m)
    else:
        final_members = list(members)

    # 记录该群本次成员快照，供后续 [3] 同步比对增删
    snapshots[target_chat_id] = list(members)
    save_snapshots(snapshots)
    upsert_env("ALLOWED_USERS", ",".join(final_members))

    print("\n" + "=" * 60)
    if is_sync_mode:
        if not sync_added and not sync_removed:
            print(f"✅ 群成员与白名单已一致（共 {len(members)} 人），无需变动。")
        else:
            print(f"🎉 同步完成：新增 {len(sync_added)} 人，移出 {len(sync_removed)} 人。当前飞书白名单共 {len(final_members)} 人。")
            if sync_added:
                shown = sync_added[:10]
                print(f"   新增: {', '.join(shown)}{' ...' if len(sync_added) > 10 else ''}")
            if sync_removed:
                shown = sync_removed[:10]
                print(f"   移出: {', '.join(shown)}{' ...' if len(sync_removed) > 10 else ''}")
    elif is_append_mode:
        print(f"🎉 已追加新群成员！当前飞书白名单共 {len(final_members)} 人已写入 .env 的 ALLOWED_USERS。")
    else:
        print(f"🎉 已重置群成员名单！共 {len(final_members)} 名群成员已写入 .env 的 ALLOWED_USERS。")
    print("💡 提示：若服务正在运行，请执行重启服务使新名单生效。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
