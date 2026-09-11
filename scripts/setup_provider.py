"""Interactive provider configuration wizard (called by MyClaw-Setup.bat).

Supports:
  - Preset providers (glm / deepseek / kimi): only the API key is needed.
  - Fully custom providers: name / base URL / API key / three model tiers
    (enter = same as previous tier, for relay providers with a single model).
  - Configure multiple providers in one run; at the end pick the active one.

Env-var compatibility (headless): MYCLAW_PROVIDER + MYCLAW_API_KEY configure
one preset non-interactively then exit. MYCLAW_CONFIG_DIR overrides the
config directory for testing.
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("MYCLAW_CONFIG_DIR") or (ROOT / "config"))
EXAMPLES_DIR = ROOT / "examples"

PRESETS = {
    "1": {
        "name": "glm",
        "label": "智谱 GLM",
        "site": "open.bigmodel.cn",
        "template": "settings_glm.example.json",
    },
    "2": {
        "name": "deepseek",
        "label": "DeepSeek",
        "site": "platform.deepseek.com",
        "template": "settings_deepseek.example.json",
    },
    "3": {
        "name": "kimi",
        "label": "Kimi (月之暗面)",
        "site": "platform.moonshot.cn",
        "template": "settings_kimi.example.json",
    },
}


def _input(prompt: str) -> str:
    """Strip + 返回空串表示跳过/EOF 由调用方决定。"""
    try:
        return input(prompt).strip()
    except EOFError:
        return "\x00EOF"


def _ask_required(prompt: str, pattern: str | None = None, hint: str = "") -> str:
    for _ in range(3):
        v = _input(prompt)
        if v == "\x00EOF":
            return ""
        if v and (pattern is None or re.fullmatch(pattern, v)):
            return v
        print(f"  输入无效{('，' + hint) if hint else ''}，请重试（Ctrl+C 可退出）。")
    return ""


def _ask_key(label: str) -> str:
    for _ in range(3):
        v = _input(f"请粘贴 {label} 的 API Key 并回车: ")
        if v == "\x00EOF":
            return ""
        if v:
            return v
        print("Key 为空，请重新粘贴。")
    return ""


def verify_key(base_url: str, key: str, model: str) -> None:
    try:
        import httpx
    except ImportError:
        print("WARN: venv 缺少 httpx，跳过 Key 联网验证。")
        return
    payload = {"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "Say OK"}]}
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    # Anthropic 系端点有的是 {base}/v1/messages，有的是 {base}/messages，
    # 404 url.not_found 视为路径不对，换下一个再试。
    for suffix in ("/v1/messages", "/messages"):
        try:
            resp = httpx.post(f"{base_url.rstrip('/')}{suffix}", json=payload, headers=headers, timeout=30)
        except Exception as exc:
            print(f"WARN: 无法联网验证 Key ({exc})")
            print("     配置已保存，可稍后在飞书中用 /provider 切换并测试。")
            return
        if resp.status_code == 200:
            print("OK: API Key 联网验证通过 (HTTP 200)")
            return
        if resp.status_code == 404 and "not_found" in resp.text:
            continue
        print(f"WARN: 供应商返回 HTTP {resp.status_code}: {resp.text[:120]}")
        print("     配置已保存，请核对 Key / 模型名是否正确。")
        return
    print("WARN: 两个常见路径都返回 404，无法确认 Key 有效性（配置已保存）。")


def write_profile(name: str, data: dict) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    target = CONFIG_DIR / f"settings_{name}.json"
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return target


def configure_preset(preset: dict) -> bool:
    name = preset["name"]
    key = _ask_key(preset["label"])
    if not key:
        print("[-] Key 未提供，跳过该供应商。")
        return False
    template = EXAMPLES_DIR / preset["template"]
    if not template.exists():
        print(f"ERROR: 模板不存在: {template}")
        return False
    data = json.loads(template.read_text("utf-8"))
    data.setdefault("env", {})["ANTHROPIC_AUTH_TOKEN"] = key
    data["label"] = preset["label"]
    target = write_profile(name, data)
    print(f"OK: 已写入 {target}")
    env = data["env"]
    model = env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL") or env.get("ANTHROPIC_DEFAULT_OPUS_MODEL", "")
    base = env.get("ANTHROPIC_BASE_URL", "")
    if base and model:
        print("正在联网验证 API Key ...")
        verify_key(base, key, model)
    return True


def configure_custom() -> bool:
    print("—— 自定义供应商 ——")
    name = _ask_required(
        "供应商名称（英文/数字/中划线，用作文件名，如 openrouter）: ",
        pattern=r"[A-Za-z0-9_-]{1,32}",
        hint="仅限英文、数字、下划线、中划线",
    )
    if not name:
        print("[-] 名称未提供，跳过。")
        return False
    name = name.lower()
    label = _input(f"显示名称（可回车跳过，默认用 {name}）: ")
    if label == "\x00EOF":
        label = ""
    base_url = _ask_required(
        "请求地址（如 https://api.example.com/anthropic ）: ",
        pattern=r"https?://[^\s]+",
        hint="必须以 http:// 或 https:// 开头",
    )
    if not base_url:
        print("[-] 请求地址未提供，跳过。")
        return False
    key = _ask_key(name)
    if not key:
        print("[-] Key 未提供，跳过。")
        return False
    m1 = _ask_required("haiku 档模型名: ")
    if not m1:
        print("[-] 模型名未提供，跳过。")
        return False
    m2 = _input("sonnet 档模型名（回车 = 同上）: ")
    if m2 in ("", "\x00EOF"):
        m2 = m1
    m3 = _input("opus 档模型名（回车 = 同上）: ")
    if m3 in ("", "\x00EOF"):
        m3 = m2 if m2 != "\x00EOF" else m1

    data = {
        "env": {
            "ANTHROPIC_AUTH_TOKEN": key,
            "ANTHROPIC_BASE_URL": base_url,
            "API_TIMEOUT_MS": "3000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": m1,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": m2,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": m3,
        },
        "model": "opus[1m]",
    }
    if label:
        data["label"] = label
    target = write_profile(name, data)
    print(f"OK: 已写入 {target}")
    print("正在联网验证 API Key ...")
    verify_key(base_url, key, m1)
    return True


def list_profiles() -> list[str]:
    if not CONFIG_DIR.exists():
        return []
    return sorted(
        p.stem.replace("settings_", "")
        for p in CONFIG_DIR.glob("settings_*.json")
    )


def pick_active(existing: list[str]) -> str | None:
    """结束后选择当前生效供应商，默认第 1 个。EOF 时用默认。"""
    if not existing:
        return None
    if len(existing) == 1:
        return existing[0]
    print("\n已配置的供应商：")
    for i, n in enumerate(existing, 1):
        print(f"  {i}. {n}")
    v = _input(f"把哪个设为当前生效供应商？(1-{len(existing)}，回车 = 1): ")
    if v == "\x00EOF" or (v and (not v.isdigit() or not (1 <= int(v) <= len(existing)))):
        if v not in ("", "\x00EOF"):
            print("  输入无效，使用默认 1。")
        v = "1"
    if v == "":
        v = "1"
    return existing[int(v) - 1]


def main() -> int:
    # headless 兼容：MYCLAW_PROVIDER + MYCLAW_API_KEY 配一个预设就退出
    env_provider = os.environ.get("MYCLAW_PROVIDER", "").strip().lower()
    env_key = os.environ.get("MYCLAW_API_KEY", "").strip()
    if env_provider and env_key:
        preset = next((p for p in PRESETS.values() if p["name"] == env_provider), None)
        if preset is None:
            print("ERROR: 未知预置供应商:", env_provider)
            return 1
        data = json.loads((EXAMPLES_DIR / preset["template"]).read_text("utf-8"))
        data.setdefault("env", {})["ANTHROPIC_AUTH_TOKEN"] = env_key
        data["label"] = preset["label"]
        target = write_profile(env_provider, data)
        (CONFIG_DIR / "active_profile").write_text(env_provider, "utf-8")
        print(f"OK: 已写入 {target} 并设为当前供应商: {preset['label']}")
        return 0

    configured_any = False
    while True:
        print("\n[?] 请选择供应商编号并回车：")
        for k, p in PRESETS.items():
            print(f"    {k}. {p['label']}  ({p['site']})")
        print("    4. 自定义供应商（手填 名称/地址/Key/三档模型）")
        choice = _input("编号（回车 = 配置完成）: ")
        if choice == "\x00EOF":
            break
        if choice == "":
            if configured_any or list_profiles():
                break
            print("  尚未配置任何供应商（没有 API Key 机器人无法对话），请先配置一个。")
            continue
        if choice in PRESETS:
            ok = configure_preset(PRESETS[choice])
        elif choice == "4":
            ok = configure_custom()
        else:
            print("  无效编号，请输入 1-4。")
            continue
        if ok:
            configured_any = True
            more = _input("\n[?] 还要配置其他供应商吗？ [y/N]: ")
            if more != "y" and more != "Y":
                break

    existing = list_profiles()
    if not existing:
        print("ERROR: 未配置任何供应商。")
        return 1
    active = pick_active(existing)
    if active is None:
        return 1
    (CONFIG_DIR / "active_profile").write_text(active, "utf-8")

    print("\n========== 供应商配置完成 ==========")
    for n in existing:
        marker = " ← 当前生效" if n == active else ""
        print(f"  {n}{marker}")
    print("提示: 飞书中可用 /provider 随时切换供应商。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
