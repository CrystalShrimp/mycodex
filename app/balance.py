from __future__ import annotations

import logging
from typing import Any

import httpx

from app.profiles import get_active_profile, load_profile_env

logger = logging.getLogger("myclaw.balance")


async def fetch_deepseek_balance(api_key: str) -> dict[str, Any]:
    """Fetch user balance from DeepSeek API (https://api.deepseek.com/user/balance)."""
    url = "https://api.deepseek.com/user/balance"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return {
                "success": False,
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
            }
        data = resp.json()
        return {
            "success": True,
            "provider": "DeepSeek",
            "is_available": data.get("is_available", True),
            "balance_infos": data.get("balance_infos", []),
        }


async def fetch_glm_balance(api_key: str) -> dict[str, Any]:
    """Fetch usage and quota limits from Zhipu GLM API (https://open.bigmodel.cn/api/monitor/usage/quota/limit)."""
    url = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return {
                "success": False,
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
            }
        data = resp.json()
        return {
            "success": True,
            "provider": "智谱 GLM",
            "raw_data": data,
        }


async def get_profile_balance(profile_name: str | None = None) -> dict[str, Any]:
    """Query balance for the given profile or currently active profile."""
    name = profile_name or get_active_profile()
    if name == "unknown":
        return {
            "success": False,
            "error": "未激活任何 Profile，请先使用 `/model` 或 `/provider` 选择模型。",
        }

    env = load_profile_env(name)
    if not env:
        return {
            "success": False,
            "error": f"Profile `{name}` 不存在或内容为空。",
        }

    base_url = env.get("ANTHROPIC_BASE_URL", "").lower()
    api_key = env.get("ANTHROPIC_AUTH_TOKEN", "").strip()

    if not api_key:
        return {
            "success": False,
            "error": f"Profile `{name}` 缺少 `ANTHROPIC_AUTH_TOKEN` (API Key)。",
        }

    name_lower = name.lower()

    if "deepseek" in name_lower or "deepseek.com" in base_url:
        res = await fetch_deepseek_balance(api_key)
        res["profile_name"] = name
        return res
    elif "glm" in name_lower or "bigmodel" in base_url or "zhipu" in name_lower:
        res = await fetch_glm_balance(api_key)
        res["profile_name"] = name
        return res
    else:
        return {
            "success": False,
            "profile_name": name,
            "error": f"当前 Profile `{name}` 对应的供应商服务暂未接入余额查询。目前支持 DeepSeek 和 智谱 GLM。",
        }
