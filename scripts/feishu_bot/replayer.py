"""飞书开发者后台自动化回放器（方案 B 第二步）。

通过 page.evaluate + 浏览器原生 fetch 发请求——cookie 由浏览器自动带，
csrf 由前端自带的 fetch 拦截器自动加，完全不用关心 token 怎么算出来的。

前置条件：
  - Chrome 用 --remote-debugging-port=9222 启动且已扫码登录飞书开发者后台
  - prev_work/feishu-permissions.json 存在（权限 scope 列表）

输出：
  - scripts/feishu_bot/artifacts/replay-result-{timestamp}.json
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("ERROR: playwright 未安装。跑：uv pip install playwright")
    sys.exit(1)

CDP_URL = "http://localhost:9222"
ROOT = Path(__file__).resolve().parent
PERMISSIONS_FILE = ROOT.parents[1] / "prev_work" / "feishu-permissions.json"
ARTIFACTS = ROOT / "artifacts"
ARTIFACTS.mkdir(exist_ok=True)

BASE = "https://open.feishu.cn"


def cdp_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


# ---------- 核心：通过浏览器 fetch 发请求 ----------

FETCH_JS = """
async ({path, body, method, csrfToken}) => {
    const headers = {'content-type': 'application/json'};
    if (csrfToken) headers['x-csrf-token'] = csrfToken;
    const r = await fetch(path, {
        method: method || 'POST',
        headers,
        body: method === 'GET' ? undefined : JSON.stringify(body || {}),
        credentials: 'include',
    });
    let data;
    const text = await r.text();
    try { data = JSON.parse(text); } catch { data = {raw: text.slice(0, 500)}; }
    return {status: r.status, data};
}
"""


async def call_api(page, path: str, body: dict | None = None, method: str = "POST", csrf_token: str = "") -> dict:
    """通过浏览器原生 fetch 发请求，cookie 自动带，csrf 用拦截到的真实 token。"""
    result = await page.evaluate(FETCH_JS, {"path": path, "body": body or {}, "method": method, "csrfToken": csrf_token})
    status = result.get("status")
    data = result.get("data") or {}
    if status != 200:
        raise RuntimeError(f"{path} HTTP {status}: {json.dumps(data, ensure_ascii=False)[:300]}")
    if data.get("code") != 0:
        raise RuntimeError(f"{path} code={data.get('code')} msg={data.get('msg') or data.get('message')}")
    return data.get("data", {}) or {}


async def capture_csrf_token(page) -> str:
    """拦截页面自己发出的 /developers/ POST 请求，读 x-csrf-token——这是 ground truth。
    通过 reload /app 触发页面初始化请求。"""
    captured: list[str] = []

    async def handler(route, request):
        if request.method == "POST" and "/developers/" in request.url and not captured:
            try:
                hdrs = await request.all_headers()
                t = hdrs.get("x-csrf-token")
                if t:
                    captured.append(t)
            except Exception:
                pass
        await route.continue_()

    await page.route("**/*", handler)
    try:
        await page.goto(f"{BASE}/app", wait_until="domcontentloaded")
        # 等 8 秒内抓到
        for _ in range(80):
            if captured:
                break
            await asyncio.sleep(0.1)
    finally:
        await page.unroute("**/*")

    if not captured:
        raise RuntimeError(
            "拦截 8 秒没抓到 /developers/ POST 请求的 x-csrf-token。"
            "页面可能没正常加载，或当前账号没登录到开发者后台。"
        )
    print(f"      抓到真实 csrf token: {captured[0][:24]}...（长度 {len(captured[0])}）")
    return captured[0]


async def diagnose_csrf(page) -> None:
    """如果 csrf 失败，dump 浏览器里 token 可能的位置，方便排查。"""
    diag = await page.evaluate("""
        () => {
            const ls = {};
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                const v = localStorage.getItem(k);
                if (k.toLowerCase().includes('csrf') || k.toLowerCase().includes('token')) {
                    ls[k] = (v || '').slice(0, 60);
                }
            }
            const ss = {};
            for (let i = 0; i < sessionStorage.length; i++) {
                const k = sessionStorage.key(i);
                if (k.toLowerCase().includes('csrf') || k.toLowerCase().includes('token')) {
                    ss[k] = (sessionStorage.getItem(k) || '').slice(0, 60);
                }
            }
            const cookieNames = document.cookie.split(';').map(c => c.split('=')[0].trim()).filter(Boolean);
            const csrfCookies = cookieNames.filter(n => n.toLowerCase().includes('csrf'));
            return {localStorage: ls, sessionStorage: ss, csrfCookies, allCookieNames: cookieNames.slice(0, 30)};
        }
    """)
    print(f"\n[诊断] localStorage csrf/token 字段: {diag.get('localStorage')}")
    print(f"[诊断] sessionStorage csrf/token 字段: {diag.get('sessionStorage')}")
    print(f"[诊断] document.cookie csrf 名: {diag.get('csrfCookies')}")
    print(f"[诊断] document.cookie 全部名: {diag.get('allCookieNames')}")


# ---------- 流程步骤 ----------

async def step_create_app(page, name: str, desc: str) -> str:
    print(f"[1/8] 创建应用：{name}")
    data = await call_api(page, "/developers/v1/app/create", {
        "appSceneType": 0,
        "name": name,
        "desc": desc,
        "i18n": {"zh_cn": {"name": name, "description": desc}},
        "primaryLang": "zh_cn",
    })
    app_id = data.get("ClientID")
    if not app_id:
        raise RuntimeError(f"create 没返回 ClientID: {data}")
    print(f"      appId = {app_id}")
    return app_id


async def step_get_secret(page, app_id: str) -> str:
    print("[2/8] 抓 App Secret")
    data = await call_api(page, f"/developers/v1/secret/{app_id}", {})
    secret = data.get("secret")
    if not secret:
        raise RuntimeError(f"secret 接口没返回 secret: {data}")
    print(f"      secret = {secret[:4]}****{secret[-4:]}")
    return secret


async def step_build_scope_map(page, app_id: str) -> dict[str, str]:
    print("[3/8] 拉 scope 全量列表")
    data = await call_api(page, f"/developers/v1/scope/all/{app_id}", {})
    scopes: list = []
    for key in ("scopes", "allScopes", "scopeList"):
        if isinstance(data.get(key), list):
            scopes = data[key]
            break
    if not scopes:
        for biz in data.get("scopeBizs", []) or []:
            for item in biz.get("items", []) or []:
                scopes.append(item)
    name_to_id: dict[str, str] = {}
    for s in scopes:
        name = s.get("name") or s.get("scopeName") or s.get("key")
        sid = s.get("id") or s.get("scopeId") or s.get("scopeID")
        if name and sid is not None:
            name_to_id[str(name)] = str(sid)
    print(f"      拿到 {len(name_to_id)} 个 scope")
    if not name_to_id:
        print(f"      [WARN] 没解析出 scope，原始 data 样本：{json.dumps(data, ensure_ascii=False)[:800]}")
    return name_to_id


async def step_import_permissions(page, app_id: str, name_to_id: dict[str, str], perms: dict) -> None:
    print("[4/8] 导入权限")
    tenant_names = (perms.get("scopes") or {}).get("tenant") or []
    user_names = (perms.get("scopes") or {}).get("user") or []
    tenant_ids, tenant_missing = [], []
    for n in tenant_names:
        sid = name_to_id.get(n)
        if sid: tenant_ids.append(sid)
        else: tenant_missing.append(n)
    user_ids, user_missing = [], []
    for n in user_names:
        sid = name_to_id.get(n)
        if sid: user_ids.append(sid)
        else: user_missing.append(n)
    if tenant_missing:
        print(f"      [WARN] tenant scope 未找到 ID ({len(tenant_missing)}/{len(tenant_names)}): {tenant_missing[:5]}")
    if user_missing:
        print(f"      [WARN] user scope 未找到 ID ({len(user_missing)}/{len(user_names)}): {user_missing[:5]}")
    print(f"      导入 tenant x {len(tenant_ids)}, user x {len(user_ids)}")
    await call_api(page, f"/developers/v1/scope/update/{app_id}", {
        "appScopeIDs": tenant_ids,
        "userScopeIDs": user_ids,
        "scopeIds": [],
        "operation": "add",
        "isDeveloperPanel": True,
    })


async def step_enable_bot(page, app_id: str) -> None:
    print("[5/8] 启用机器人能力")
    await call_api(page, f"/developers/v1/robot/switch/{app_id}", {"enable": True})


async def step_configure_event(page, app_id: str, event_names: list[str]) -> None:
    print(f"[6/8] 配置事件订阅（WebSocket）+ 添加 {len(event_names)} 个事件")
    await call_api(page, f"/developers/v1/event/switch/{app_id}", {"eventMode": 4})
    await call_api(page, f"/developers/v1/callback/switch/{app_id}", {"callbackMode": 4})
    await call_api(page, f"/developers/v1/event/update/{app_id}", {
        "operation": "add",
        "events": [],
        "appEvents": event_names,
        "userEvents": [],
        "eventMode": 4,
    })


async def step_create_version(page, app_id: str, version: str, change_log: str, owner_member_id: str | None) -> str:
    print(f"[7/8] 创建版本 {version}")
    visible_suggest = {
        "departments": [],
        "members": [owner_member_id] if owner_member_id else [],
        "groups": [],
        "isAll": 1 if not owner_member_id else 0,
    }
    data = await call_api(page, f"/developers/v1/app_version/create/{app_id}", {
        "appVersion": version,
        "mobileDefaultAbility": "bot",
        "pcDefaultAbility": "bot",
        "changeLog": change_log,
        "visibleSuggest": visible_suggest,
        "applyReasonConfig": {
            "apiPrivilegeNeedReason": False, "contactPrivilegeNeedReason": False,
            "dataPrivilegeReasonMap": {}, "visibleScopeNeedReason": False,
            "apiPrivilegeReasonMap": {}, "contactPrivilegeReason": "",
            "isDataPrivilegeExpandMap": {}, "visibleScopeReason": "",
            "dataPrivilegeNeedReason": False, "isAutoAudit": False, "isContactExpand": False,
        },
        "b2cShareSplitConfigSuggest": {
            "b2cGroupChatShareEnable": False, "b2cP2PChatShareEnable": False, "b2cP2PChatNeedAudit": False,
        },
        "autoPublish": False,
        "blackVisibleSuggest": {"departments": [], "members": [], "groups": [], "isAll": 0},
    })
    version_id = data.get("versionId")
    if not version_id:
        raise RuntimeError(f"create version 没返回 versionId: {data}")
    print(f"      versionId = {version_id}")
    return str(version_id)


async def step_publish(page, app_id: str, version_id: str) -> None:
    print("[8/8] 提交发布")
    await call_api(page, f"/developers/v1/publish/commit/{app_id}/{version_id}", {})


# ---------- 主入口 ----------

async def main():
    if not cdp_alive():
        print("Chrome CDP (localhost:9222) 没开。先按 recorder.py 的说明启动 Chrome。")
        sys.exit(1)

    app_name = "OpenClaw 助手"
    app_desc = "用于接入 OpenClaw 的飞书机器人"
    version = "1.0.0"
    change_log = "首次发版，包含模板所需权限"
    event_names = ["im.message.receive_v1", "im.message.message_read_v1"]
    owner_member_id = None

    perms = json.loads(PERMISSIONS_FILE.read_text(encoding="utf-8"))
    print(f"加载权限：tenant={len(perms['scopes']['tenant'])} user={len(perms['scopes']['user'])}")

    print("\n=== 连 Chrome，找/开飞书 tab ===")
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            raise RuntimeError("Chrome 里没有 user context")
        ctx = browser.contexts[0]
        # 找一个 open.feishu.cn 的 tab；没有就开一个
        page = None
        for pg in ctx.pages:
            if "open.feishu.cn" in pg.url:
                page = pg
                break
        if not page:
            page = await ctx.new_page()
            await page.goto(f"{BASE}/app", wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle", timeout=15000)
        else:
            # 已经在飞书页，确保是登录态（URL 含 /app 或 /application 等）
            if "/app" not in page.url and "/application" not in page.url:
                await page.goto(f"{BASE}/app", wait_until="domcontentloaded")
                await page.wait_for_load_state("networkidle", timeout=15000)
        print(f"      用 tab: {page.url}")

        result = {
            "started_at": datetime.now().isoformat(),
            "app_name": app_name,
            "steps_completed": [],
            "warnings": [],
        }
        try:
            app_id = await step_create_app(page, app_name, app_desc)
            result["appId"] = app_id
            result["steps_completed"].append("create_app")

            secret = await step_get_secret(page, app_id)
            result["appSecret"] = secret
            result["steps_completed"].append("get_secret")

            scope_map = await step_build_scope_map(page, app_id)
            result["scope_map_size"] = len(scope_map)
            result["steps_completed"].append("build_scope_map")

            await step_import_permissions(page, app_id, scope_map, perms)
            result["steps_completed"].append("import_permissions")

            await step_enable_bot(page, app_id)
            result["steps_completed"].append("enable_bot")

            await step_configure_event(page, app_id, event_names)
            result["steps_completed"].append("configure_event")

            version_id = await step_create_version(page, app_id, version, change_log, owner_member_id)
            result["versionId"] = version_id
            result["steps_completed"].append("create_version")

            await step_publish(page, app_id, version_id)
            result["steps_completed"].append("publish")

            result["status"] = "completed"
        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            msg = str(e)
            if "csrf" in msg.lower() or "9499" in msg:
                print("\n[提示] csrf 失败。正在 dump 浏览器存储供诊断...")
                try:
                    await diagnose_csrf(page)
                except Exception as diag_err:
                    print(f"[诊断失败] {diag_err}")
            print(f"\n[FAIL] {e}")
            raise
        finally:
            result["finished_at"] = datetime.now().isoformat()
            out = ARTIFACTS / f"replay-result-{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n结果写入：{out}")

    if result.get("status") == "completed":
        print("\n=== ✅ 全流程完成 ===")
        print(f"App ID:     {result.get('appId')}")
        print(f"App Secret: {result.get('appSecret','')[:4]}****{result.get('appSecret','')[-4:]}")


if __name__ == "__main__":
    asyncio.run(main())
