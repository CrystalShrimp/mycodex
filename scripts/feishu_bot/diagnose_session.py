"""飞书会话诊断脚本（按 help.md 方案）。

**只读诊断，不做任何配置变更**：
1. 被动监听当前 open.feishu.cn 页面，捕获真实的 x-csrf-token
2. 用同源 fetch 验证 /scope/all/{appId} 完整响应
3. 分析响应结构，找出 scope key → 数字 ID 映射
4. 对比 prev_work/feishu-permissions.json，输出匹配报告

硬性约束：
- 只用 connect_over_cdp，不下浏览器
- 不调 page.goto()
- 不创建应用、不改权限、不发布
- 不打印完整 cookie / App Secret / CSRF token
- 报告里只显示 token 前 4 + 后 4

用法：
  1. Chrome 已经启动且 --remote-debugging-port=9222，登录飞书后台
  2. 在 Chrome 里手动切到一个飞书开发者后台的 tab（任何 /app/cli_xxx/* 页面都行）
  3. 跑：python scripts/feishu_bot/diagnose_session.py
  4. 看到提示后，在 Chrome 里手动点开任一应用的「权限管理」页
  5. 等脚本抓到请求并自动跑完诊断
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("ERROR: playwright 未安装。跑：uv pip install playwright")
    sys.exit(1)

CDP_URL = "http://localhost:9222"
ROOT = Path(__file__).resolve().parent
PERMISSIONS_FILE = ROOT.parents[1] / "prev_work" / "feishu-permissions.json"
RECORDING_FILE = ROOT / "recording" / "events.jsonl"
ARTIFACTS = ROOT / "artifacts"
ARTIFACTS.mkdir(exist_ok=True)
SCOPE_ALL_OUT = ARTIFACTS / "scope-all-full.json"

# 候选 scope 字段名（按 help.md）
ID_KEYS = ("id", "scopeId", "scopeID")
NAME_KEYS = ("key", "name", "scope", "scopeName", "apiName", "permission")


def cdp_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def mask_token(t: str) -> str:
    if not t:
        return "(empty)"
    if len(t) <= 8:
        return "*" * len(t)
    return f"{t[:4]}...{t[-4:]}"


# ---------- 1. 被动监听 ----------

class CaptureState:
    def __init__(self):
        self.token: str = ""              # 完整 token 只存在内存
        self.real_request_headers: dict = {}  # 真实请求的所有 header 名（不含值）
        self.real_request_url: str = ""
        self.real_request_initiator: dict = {}
        self.app_id: str = ""
        self.cdp_request_meta: dict[str, dict] = {}  # requestId -> {url, headers, initiator}
        self.extra_info_headers: dict[str, dict] = {}  # requestId -> headers (full)

    def try_resolve_token(self):
        """合并 cdp_request_meta 和 extra_info_headers，按 requestId 找 csrf。"""
        for rid, meta in self.cdp_request_meta.items():
            extra = self.extra_info_headers.get(rid, {})
            # 合并两边的 headers，extra 优先（含浏览器加的 cookie 等）
            merged = {**meta.get("headers", {}), **extra}
            # 找 csrf（大小写不敏感）
            csrf_val = ""
            for k, v in merged.items():
                if k.lower() == "x-csrf-token":
                    csrf_val = v
                    break
            if csrf_val:
                self.token = csrf_val
                self.real_request_headers = {k: "<hidden>" for k in merged}
                self.real_request_url = meta.get("url", "")
                self.real_request_initiator = meta.get("initiator", {})
                # 从 URL 提取 appId：/developers/v1/.../cli_xxx
                url = self.real_request_url
                if "cli_" in url:
                    import re
                    m = re.search(r"(cli_[a-f0-9]+)", url)
                    if m:
                        self.app_id = m.group(1)
                return True
        return False


def find_feishu_page(browser):
    """只找已存在的 open.feishu.cn 页面，不导航。"""
    for ctx in browser.contexts:
        for pg in ctx.pages:
            if "open.feishu.cn" in (pg.url or ""):
                return ctx, pg
    return None, None


async def setup_passive_listeners(ctx, page, state: CaptureState):
    """注册多通道被动监听，不拦截不修改。"""

    # 通道 1+2: Playwright 高层 request 事件（可能拿不到自定义头，但能拿到 URL）
    def on_request(req):
        if req.method != "POST":
            return
        if "/developers/v1/" not in req.url:
            return
        # 高层 API 拿到的 headers 有限，但还是记一下
        # 不在这里直接解析 token，等 CDP extraInfo 来合并
        if "cli_" in req.url and not state.app_id:
            import re
            m = re.search(r"(cli_[a-f0-9]+)", req.url)
            if m:
                state.app_id = m.group(1)

    ctx.on("request", on_request)
    page.on("request", on_request)

    # 通道 3+4: CDP 原生事件（含浏览器实际发送的完整 headers）
    cdp = await ctx.new_cdp_session(page)
    await cdp.send("Network.enable")

    def on_request_will_be_sent(params):
        req = params.get("request", {})
        url = req.get("url", "")
        if req.get("method") != "POST" or "/developers/v1/" not in url:
            return
        rid = params.get("requestId")
        state.cdp_request_meta[rid] = {
            "url": url,
            "headers": req.get("headers", {}),
            "initiator": params.get("initiator", {}),
        }
        # 尝试合并解析
        if not state.token:
            state.try_resolve_token()

    def on_request_will_be_sent_extra_info(params):
        rid = params.get("requestId")
        headers = params.get("headers", {})
        state.extra_info_headers[rid] = headers
        # 尝试合并解析
        if not state.token:
            state.try_resolve_token()
        # 清理太老的可能漏配对的（保留最近 200 个）
        if len(state.extra_info_headers) > 200:
            oldest = list(state.extra_info_headers.keys())[:100]
            for k in oldest:
                del state.extra_info_headers[k]
                state.cdp_request_meta.pop(k, None)

    cdp.on("Network.requestWillBeSent", on_request_will_be_sent)
    cdp.on("Network.requestWillBeSentExtraInfo", on_request_will_be_sent_extra_info)

    return cdp


async def fallback_page_route(ctx, page, state: CaptureState):
    """如果高层事件拿不到 token，用 page.route 兜底——只读，不修改。"""
    print("[兜底] 启用 page.route 拦截读取（只读）...")
    done = asyncio.Event()

    async def handler(route, request):
        if not done.is_set() and request.method == "POST" and "/developers/v1/" in request.url:
            try:
                all_hdrs = await request.all_headers()
                for k, v in all_hdrs.items():
                    if k.lower() == "x-csrf-token":
                        state.token = v
                        state.real_request_url = request.url
                        state.real_request_headers = {k: "<hidden>" for k in all_hdrs}
                        if "cli_" in request.url and not state.app_id:
                            import re
                            m = re.search(r"(cli_[a-f0-9]+)", request.url)
                            if m:
                                state.app_id = m.group(1)
                        done.set()
                        break
            except Exception as e:
                print(f"[兜底] 读 headers 失败: {e}")
        await route.continue_()

    await page.route("**/*", handler)
    try:
        await asyncio.wait_for(done.wait(), timeout=60)
    except asyncio.TimeoutError:
        pass
    finally:
        await page.unroute("**/*", handler)


# ---------- 2. 用同源 fetch 验证 /scope/all ----------

TEST_SCOPE_ALL_JS = """
async ({appId, token}) => {
    const r = await fetch(`/developers/v1/scope/all/${appId}`, {
        method: "POST",
        credentials: "include",
        headers: {
            "content-type": "application/json",
            "x-csrf-token": token
        },
        body: "{}"
    });
    let data;
    const text = await r.text();
    try { data = JSON.parse(text); } catch { data = {raw: text.slice(0, 500)}; }
    return {status: r.status, data};
}
"""


async def test_scope_all(page, token: str, app_id: str) -> dict:
    print(f"\n[测试] POST /developers/v1/scope/all/{app_id}")
    result = await page.evaluate(TEST_SCOPE_ALL_JS, {"appId": app_id, "token": token})
    # 保存完整响应（不含 cookie/token——这俩本来就不在 response body 里）
    SCOPE_ALL_OUT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  HTTP {result.get('status')}, code={result.get('data', {}).get('code')}")
    print(f"  完整响应写入: {SCOPE_ALL_OUT}")
    return result


# ---------- 3. 分析 scope/all 响应结构 ----------

def find_array_paths(obj, path="", out: list | None = None):
    """递归找出所有数组字段的 path + 第一项。"""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_path = f"{path}.{k}" if path else k
            if isinstance(v, list) and v:
                out.append((new_path, v[0] if v else None, len(v)))
            find_array_paths(v, new_path, out)
    elif isinstance(obj, list):
        # 只看前 2 个 item 避免爆栈
        for item in obj[:2]:
            find_array_paths(item, path, out)
    return out


def find_scope_candidates(data):
    """找包含 id 类字段 + name 类字段的数组路径（候选 scope 列表）。"""
    candidates = []
    for path, first, count in find_array_paths(data):
        if not isinstance(first, dict):
            continue
        keys = set(first.keys())
        id_hits = keys & set(ID_KEYS)
        name_hits = keys & set(NAME_KEYS)
        if id_hits and name_hits:
            candidates.append({
                "path": path,
                "count": count,
                "first_item_keys": sorted(keys),
                "id_fields": sorted(id_hits),
                "name_fields": sorted(name_hits),
                "first_item_sample": {k: first.get(k) for k in list(keys)[:8]},
            })
    return candidates


def build_scope_map(data, candidate):
    """根据选定的候选 path 构建 scope key → id 字典。"""
    # 沿 path 取到那个数组
    parts = candidate["path"].split(".")
    arr = data
    for p in parts:
        if isinstance(arr, dict):
            arr = arr.get(p)
        elif isinstance(arr, list):
            arr = [x.get(p) for x in arr if isinstance(x, dict)]
    if not isinstance(arr, list):
        return {}

    # 选一个 id 字段和一个 name 字段
    id_field = candidate["id_fields"][0]  # 简单取第一个
    # 优先用 "key" 字段（scope key 通常叫 key 而不是 name）
    name_field = "key" if "key" in candidate["name_fields"] else candidate["name_fields"][0]

    out = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        sk = item.get(name_field)
        sid = item.get(id_field)
        if sk and sid is not None:
            out[str(sk)] = str(sid)
    return out, name_field, id_field


def match_permissions(scope_map: dict, perms: dict):
    """对 feishu-permissions.json 做精确匹配检查。"""
    result = {"tenant": {}, "user": {}}
    for kind in ("tenant", "user"):
        names = (perms.get("scopes") or {}).get(kind) or []
        matched = {}
        unmatched = []
        ambiguous = []
        seen_ids = {}
        for n in names:
            ids = [v for k, v in scope_map.items() if k == n]
            if len(ids) == 0:
                unmatched.append(n)
            elif len(ids) > 1:
                ambiguous.append((n, ids))
            else:
                matched[n] = ids[0]
                seen_ids.setdefault(ids[0], []).append(n)
        duplicates = {k: v for k, v in seen_ids.items() if len(v) > 1}
        result[kind] = {
            "total": len(names),
            "matched": len(matched),
            "unmatched": unmatched,
            "ambiguous": ambiguous,
            "duplicate_ids": duplicates,
        }
    return result


# ---------- 4. appId 兜底：从录制数据找 ----------

def find_app_id_from_recording() -> str:
    if not RECORDING_FILE.exists():
        return ""
    import re
    with open(RECORDING_FILE, encoding="utf-8") as f:
        for line in f:
            m = re.search(r"(cli_[a-f0-9]+)", line)
            if m:
                return m.group(1)
    return ""


# ---------- 5. 主入口 ----------

async def main():
    if not cdp_alive():
        print("Chrome CDP (localhost:9222) 没开。")
        sys.exit(1)

    print("=== 飞书会话诊断（只读，不动配置） ===\n")

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        ctx, page = find_feishu_page(browser)
        if not page:
            print("[FAIL] 没找到 open.feishu.cn 的 tab。")
            print("       请先在 Chrome 里手动切到一个飞书开发者后台的页面，再跑这个脚本。")
            print("       （脚本不会自动导航）")
            sys.exit(1)
        print(f"Feishu page: {page.url}")

        state = CaptureState()
        cdp = await setup_passive_listeners(ctx, page, state)

        # 兜底：从录制数据找 appId，万一监听期间没出现
        recording_app_id = find_app_id_from_recording()
        if recording_app_id:
            print(f"录制数据中的 appId: {recording_app_id}")

        print("\n现在请你在 Chrome 里手动操作：")
        print("  打开任一应用的「权限管理」页（或任何会触发 /developers/v1/ 请求的页面）")
        print("  脚本会被动捕获，不会点击/导航/拦截")
        print("\n等待捕获（最多 90 秒）...\n")

        # 等最多 90 秒
        for i in range(90):
            if state.token:
                break
            await asyncio.sleep(1)
            if i % 10 == 9 and not state.token:
                print(f"  [{i+1}s] 还没抓到，请确认在 Chrome 里操作了...")

        # 兜底：page.route
        if not state.token:
            print("\n[通道 1-4 没抓到，启用 page.route 兜底 60s]")
            await fallback_page_route(ctx, page, state)

        # 选择 appId 优先级：监听抓到的 > 页面 URL > 录制
        if not state.app_id:
            import re
            m = re.search(r"(cli_[a-f0-9]+)", page.url or "")
            if m:
                state.app_id = m.group(1)
        if not state.app_id:
            state.app_id = recording_app_id

        # ---------- 报告 ----------
        print("\n" + "=" * 50)
        print("=== 诊断结果 ===")
        print("=" * 50)
        print(f"Feishu page: 已找到 ({page.url[:80]})")
        print(f"真实 developers POST: {'已捕获' if state.real_request_url else '未捕获'}")
        print(f"x-csrf-token: {'已捕获' if state.token else '未捕获'}")
        print(f"token length: {len(state.token)}")
        if state.token:
            print(f"token (masked): {mask_token(state.token)}")
        print(f"真实请求 header 名: {sorted(state.real_request_headers.keys())[:20]}")
        if state.real_request_initiator:
            print(f"initiator: {state.real_request_initiator}")
        print(f"appId (用于后续验证): {state.app_id or '(无)'}")

        if not state.token:
            print("\n[结论] 没抓到 csrf token，请检查：")
            print("  1. Chrome tab 是否真的在 open.feishu.cn")
            print("  2. 是否登录了开发者后台（不只是飞书主站）")
            print("  3. 在 Chrome 里是否真的点了切换页面/打开应用")
            await cdp.detach()
            return

        if not state.app_id:
            print("\n[结论] 没找到可用的 appId。请先在 Chrome 里打开任一应用页，再重跑。")
            await cdp.detach()
            return

        # ---------- 验证 /scope/all ----------
        result = await test_scope_all(page, state.token, state.app_id)
        data = result.get("data") or {}
        code = data.get("code")

        if code != 0:
            print(f"\n[FAIL] /scope/all 返回 code={code} msg={data.get('msg')}")
            print("\n停止——不继续枚举 cookie 或猜 token 算法。")
            print("下一步：对比真实请求和我们的测试请求的 header 差异。")
            print("（真实请求 header 名已在上面列出，测试请求只发了 content-type + x-csrf-token）")
            await cdp.detach()
            return

        # ---------- 分析响应结构 ----------
        print("\n=== /scope/all 响应结构分析 ===")
        all_arrays = find_array_paths(data)
        print(f"\n找到 {len(all_arrays)} 个数组字段：")
        for path, first, count in all_arrays[:30]:
            keys = sorted(first.keys())[:10] if isinstance(first, dict) else str(type(first))
            print(f"  {path}  (len={count}, first item keys={keys})")

        candidates = find_scope_candidates(data)
        print(f"\n含 id+name 字段的候选 scope 数组：{len(candidates)} 个")
        for c in candidates[:10]:
            print(f"  - {c['path']} count={c['count']} id_fields={c['id_fields']} name_fields={c['name_fields']}")

        if not candidates:
            print("\n[FAIL] 没找到候选 scope 数组。dump 一段 raw 数据看看：")
            print(json.dumps(data, ensure_ascii=False)[:1500])
            await cdp.detach()
            return

        # 选第一个候选建 map
        first = candidates[0]
        scope_map, name_field, id_field = build_scope_map(data, first)
        print(f"\n用 candidate {first['path']} 构建 scope_map:")
        print(f"  name 字段: {name_field}  id 字段: {id_field}")
        print(f"  scope_map 大小: {len(scope_map)}")

        # ---------- 匹配 feishu-permissions.json ----------
        print("\n=== 匹配 feishu-permissions.json ===")
        perms = json.loads(PERMISSIONS_FILE.read_text(encoding="utf-8"))
        match_result = match_permissions(scope_map, perms)
        for kind in ("tenant", "user"):
            r = match_result[kind]
            print(f"\n[{kind}] {r['matched']}/{r['total']} 匹配")
            if r["unmatched"]:
                print(f"  未匹配 ({len(r['unmatched'])}): {r['unmatched']}")
            if r["ambiguous"]:
                print(f"  歧义项: {r['ambiguous']}")
            if r["duplicate_ids"]:
                print(f"  重复 ID: {r['duplicate_ids']}")

        # 整体验收
        tenant_ok = match_result["tenant"]["matched"] == match_result["tenant"]["total"]
        user_ok = match_result["user"]["matched"] == match_result["user"]["total"]
        no_unmatched = not match_result["tenant"]["unmatched"] and not match_result["user"]["unmatched"]
        no_ambiguous = not match_result["tenant"]["ambiguous"] and not match_result["user"]["ambiguous"]
        passed = tenant_ok and user_ok and no_unmatched and no_ambiguous

        print("\n" + "=" * 50)
        print(f"scope 映射验证: {'✅ 通过' if passed else '❌ 未通过'}")
        print("本轮是否发送配置变更请求: 否（仅 /scope/all 只读）")
        print("=" * 50)

        await cdp.detach()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n中断。")
