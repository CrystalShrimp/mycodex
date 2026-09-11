"""飞书开发者后台流程录制器（方案 B 第一步）。

连接到用户已登录的 Chrome（通过 CDP），录制：
  - 每次 DOM 点击 / 表单 change 的元素定位路径
  - 每个网络请求（method/url/headers/body）和响应（status/body）
  - 每次主框架导航时的 DOM 快照（存单独 HTML 文件）

输出：
  scripts/feishu_bot/recording/events.jsonl   流式事件
  scripts/feishu_bot/recording/snapshots/*.html  每个 nav 的 DOM

用法见打印。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("ERROR: playwright 未安装。请跑：")
    print("  uv pip install playwright")
    print("(只装 Python 包，不会下载浏览器——我们走 connect_over_cdp)")
    sys.exit(1)

CDP_URL = "http://localhost:9222"
ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "recording"
SNAP_DIR = OUT_DIR / "snapshots"
LOG_FILE = OUT_DIR / "events.jsonl"
OUT_DIR.mkdir(parents=True, exist_ok=True)
SNAP_DIR.mkdir(exist_ok=True)

# 注入到每个页面的监听器。捕获 click 和 change，把元素路径序列化后
# 通过 console.log 发回 Python 端（Runtime.consoleAPICalled）。
LISTENER_JS = r"""
(() => {
  if (window.__feishu_rec_installed) return;
  window.__feishu_rec_installed = true;

  const describe = (el) => {
    if (!el || el === document.body || el === document.documentElement) return null;
    const path = [];
    let cur = el;
    for (let i = 0; i < 7 && cur && cur !== document.body; i++) {
      let cls = null;
      try { cls = typeof cur.className === 'string' ? cur.className.slice(0, 200) : null; } catch(_) {}
      let txt = null;
      try { txt = (cur.textContent || '').trim().slice(0, 80); } catch(_) {}
      path.push({
        tag: (cur.tagName || '').toLowerCase(),
        id: cur.id || null,
        cls,
        role: cur.getAttribute ? cur.getAttribute('role') : null,
        type: cur.getAttribute ? cur.getAttribute('type') : null,
        name: cur.getAttribute ? cur.getAttribute('name') : null,
        'data-testid': cur.getAttribute ? cur.getAttribute('data-testid') : null,
        href: cur.tagName === 'A' ? cur.href : null,
        value: cur.tagName === 'INPUT' || cur.tagName === 'TEXTAREA' || cur.tagName === 'SELECT'
               ? (cur.value || '').slice(0, 200) : null,
        text: txt,
      });
      cur = cur.parentElement;
    }
    // 也记录 element 在 document 中的索引（同标签的兄弟里第几个），便于 xpath 重建
    let el2 = el;
    try {
      const idx = Array.from(el2.parentElement?.children || []).indexOf(el2);
      path[0] = path[0] || {};
      path[0].siblingIndex = idx;
    } catch(_) {}
    return path;
  };

  const emit = (kind, payload) => {
    try {
      console.log(JSON.stringify({ __rec: kind, ...payload, url: location.href, ts: Date.now() }));
    } catch(_) {}
  };

  document.addEventListener('click', (e) => {
    emit('click', { path: describe(e.target), x: e.clientX, y: e.clientY });
  }, true);

  document.addEventListener('change', (e) => {
    emit('change', { path: describe(e.target), value: (e.target?.value || '').slice(0, 500) });
  }, true);

  // 记录 keydown Enter（提交表单常用）
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      emit('enter', { path: describe(e.target) });
    }
  }, true);

  console.log(JSON.stringify({ __rec: 'ping', url: location.href, ts: Date.now() }));
})();
"""


def log_event(event: dict) -> None:
    event.setdefault("_t", time.time())
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    # 同时短摘要到 stdout
    kind = event.get("type", "?")
    if kind == "request":
        m = event.get("method", "")
        u = event.get("url", "")[:110]
        print(f"  [REQ] {m:5s} {u}")
    elif kind == "response":
        s = event.get("status", "")
        u = event.get("url", "")[:110]
        if not any(skip in u for skip in (".css", ".js?", ".png", ".jpg", ".svg", ".woff", "rum:", "static")):
            print(f"  [RES] {s} {u}")
    elif kind == "click":
        path = event.get("data", {}).get("path") or []
        top = path[0] if path else {}
        label = top.get("text") or top.get("role") or top.get("tag") or "?"
        print(f"  [CLK] {label[:40]}  @ {event.get('url','')[:80]}")
    elif kind == "nav":
        print(f"  [NAV] {event.get('url','')[:110]}")


def cdp_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def find_chrome() -> str | None:
    for cand in [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]:
        if os.path.isfile(cand):
            return cand
    return None


def print_start_hint() -> None:
    print("=" * 60)
    print("未检测到 CDP（localhost:9222）。请先启动一个带调试端口的 Chrome：")
    print()
    chrome = find_chrome()
    if chrome:
        profile = ROOT / "chrome_profile"
        print(f'  "{chrome}" --remote-debugging-port=9222 --user-data-dir="{profile}"')
    else:
        print('  chrome.exe --remote-debugging-port=9222 --user-data-dir="<一个空目录>"')
    print()
    print("Chrome 启动后：")
    print("  1. 在新 Chrome 里打开 https://open.feishu.cn 扫码登录")
    print("  2. 回到这个终端，重新跑 recorder.py")
    print("=" * 60)


async def snapshot(page, label: str) -> None:
    try:
        html = await page.evaluate("() => document.documentElement.outerHTML")
        ts = datetime.now().strftime("%H%M%S_%f")[:-3]
        idx = len(list(SNAP_DIR.glob("*.html"))) + 1
        fname = SNAP_DIR / f"{idx:03d}_{label}_{ts}.html"
        fname.write_text(html, encoding="utf-8")
        log_event({"type": "snapshot", "file": str(fname.relative_to(ROOT)), "url": page.url})
    except Exception as e:
        log_event({"type": "snapshot_error", "error": str(e), "url": page.url})


def attach(page) -> None:
    async def on_console(msg):
        txt = msg.text
        if not txt.startswith('{"__rec"'):
            return
        try:
            data = json.loads(txt)
        except Exception:
            return
        kind = data.pop("__rec", None)
        if kind == "ping":
            return
        log_event({"type": kind, "data": data, "url": page.url})

    def on_request(req):
        # 跳过静态资源，否则噪音太大
        u = req.url
        if any(s in u for s in (".css", ".woff", ".png", ".jpg", ".jpeg", ".svg", ".ico", "favicon")):
            return
        log_event({
            "type": "request",
            "method": req.method,
            "url": u,
            "headers": dict(req.headers),
            "post_data": req.post_data,
        })

    async def on_response(resp):
        u = resp.url
        if any(s in u for s in (".css", ".woff", ".png", ".jpg", ".jpeg", ".svg", ".ico")):
            return
        body = None
        try:
            ct = resp.headers.get("content-type", "")
            if resp.request.method in ("POST", "PUT", "PATCH", "DELETE") or "json" in ct:
                try:
                    body = await resp.text()
                    if body and len(body) > 30000:
                        body = body[:30000] + "...[truncated]"
                except Exception:
                    pass
        except Exception:
            pass
        log_event({
            "type": "response",
            "status": resp.status,
            "method": resp.request.method,
            "url": u,
            "headers": dict(resp.headers),
            "body": body,
        })

    page.on("console", lambda msg: asyncio.create_task(on_console(msg)))
    page.on("request", on_request)
    page.on("response", lambda resp: asyncio.create_task(on_response(resp)))
    page.on("framenavigated", lambda frame: (
        asyncio.create_task(_on_nav(page, frame)) if frame == page.main_frame else None
    ))


async def _on_nav(page, frame):
    log_event({"type": "nav", "url": page.url})
    await asyncio.sleep(0.6)  # 等 SPA 渲染
    await snapshot(page, "nav")


async def main():
    if not cdp_alive():
        print_start_hint()
        sys.exit(1)

    print(f"连到 {CDP_URL} ...")
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            print("ERROR: Chrome 里没有 user context，先在 Chrome 里打开任何页面再试。")
            sys.exit(1)
        context = browser.contexts[0]

        # 选飞书 tab；没有就新开
        page = None
        for pg in context.pages:
            if "feishu.cn" in pg.url or "larksuite" in pg.url:
                page = pg
                break
        if not page:
            page = await context.new_page()
            await page.goto("https://open.feishu.cn/app")

        # 监听所有现有 tab 和未来 tab
        await context.add_init_script(LISTENER_JS)
        for pg in context.pages:
            attach(pg)
            try:
                await pg.evaluate(LISTENER_JS)
            except Exception:
                pass
        context.on("page", lambda pg: asyncio.create_task(_on_new_page(pg)))

        await snapshot(page, "init")
        print()
        print("=" * 60)
        print("✅ 录制中。在 Chrome 里走完整流程：")
        print("  创建企业自建应用 → 填应用名/描述 → 创建")
        print("  → 进应用 → 权限管理 → 勾若干权限 → 申请开通")
        print("  → （可选）版本管理 → 创建版本 → 申请发布")
        print()
        print("  走完之后按 Ctrl-C 停止。")
        print(f"  录制文件: {LOG_FILE}")
        print("=" * 60)
        print()

        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    # 摘要
    print()
    print("=" * 60)
    print("录制结束。事件流：", LOG_FILE)
    print("DOM 快照目录：", SNAP_DIR)
    n = sum(1 for _ in open(LOG_FILE, encoding="utf-8")) if LOG_FILE.exists() else 0
    print(f"总事件数: {n}")
    print("请把这两个东西发我，我来分析+写回放器。")
    print("=" * 60)


async def _on_new_page(pg):
    await pg.wait_for_load_state("domcontentloaded", timeout=5000)
    attach(pg)
    try:
        await pg.evaluate(LISTENER_JS)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n停止。")
