"""个人用白名单自动绑定。

飞书 self_manage 权限随版本发布后，OpenAPI 鉴权中心生效可能有数分钟到更久
的延迟；向导在会话内解析不到创建者时会置 FEISHU_ALLOWLIST_PENDING=1。
服务启动后由本模块后台轮询，权限一生效即自动解析创建者、写入 .env、
热更新运行中的白名单，并主动私聊通知创建者——全程无需人工干预。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx

from config.settings import settings

logger = logging.getLogger("mycodex.feishu.personal_bind")

_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_APP_INFO_URL = "https://open.feishu.cn/open-apis/application/v6/applications/{app_id}?lang=zh_cn"
_SEND_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"

_POLL_INTERVAL = 60.0
_MAX_HOURS = 24


def _env_path() -> Path:
    return Path(__file__).resolve().parents[2] / ".env"


def _upsert_env(key: str, value: str) -> None:
    path = _env_path()
    lines = path.read_text("utf-8").splitlines() if path.exists() else []
    line = f"{key}={value}"
    for i, cur in enumerate(lines):
        if cur.startswith(f"{key}=") or cur.strip() == key:
            lines[i] = line
            break
    else:
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", "utf-8")


async def _tenant_token(client: httpx.AsyncClient, app_id: str, app_secret: str) -> str:
    resp = await client.post(_TOKEN_URL, json={"app_id": app_id, "app_secret": app_secret})
    token = resp.json().get("tenant_access_token", "")
    if not token:
        raise RuntimeError(f"获取 tenant_access_token 失败：{resp.text[:120]}")
    return token


async def fetch_creator_open_id(app_id: str, app_secret: str) -> str:
    """返回当前应用作用域下的创建者 open_id；权限未生效时抛 RuntimeError。"""
    async with httpx.AsyncClient(timeout=15) as client:
        token = await _tenant_token(client, app_id, app_secret)
        resp = await client.get(
            _APP_INFO_URL.format(app_id=app_id),
            headers={"Authorization": f"Bearer {token}"},
        )
        data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"code={data.get('code')} {str(data.get('msg'))[:100]}")
    creator = ((data.get("data", {}).get("app", {}) or {}).get("creator_id") or "").strip()
    if not creator:
        raise RuntimeError("应用信息中未返回 creator_id")
    return creator


async def _notify_creator(app_id: str, app_secret: str, open_id: str, text: str) -> None:
    # 新应用发送权限可能仍在 OpenAPI 传播（99991672），带重试确保通知送达
    try:
        for _ in range(6):
            async with httpx.AsyncClient(timeout=15) as client:
                token = await _tenant_token(client, app_id, app_secret)
                resp = await client.post(
                    _SEND_MSG_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": open_id,
                        "msg_type": "text",
                        "content": json.dumps({"text": text}),
                    },
                )
                if resp.json().get("code") == 0:
                    return
            await asyncio.sleep(30)
    except Exception as e:  # 通知失败不影响配置本身
        logger.info("自动绑定完成通知发送失败（不影响配置）：%s", e)


def is_pending() -> bool:
    return settings.feishu_allowlist_pending.strip() == "1"



async def bind_from_message(open_id: str) -> bool:
    """个人模式下应用可用范围=仅创建者（自动化已配置并校验），首个私聊用户即创建者本人。"""
    try:
        settings.allowed_users = open_id
        _upsert_env("ALLOWED_USERS", open_id)
        settings.feishu_allowlist_pending = ""
        _upsert_env("FEISHU_ALLOWLIST_PENDING", "")
        logger.info("个人用白名单已由首个私聊自动绑定创建者：%s", open_id)
        return True
    except Exception as e:
        logger.warning("首个私聊自动绑定失败：%s", e)
        return False


async def bind_loop() -> None:
    app_id, app_secret = settings.feishu_app_id, settings.feishu_app_secret
    logger.info(
        "个人用白名单自动绑定已启动：等待飞书 self_manage 权限对 OpenAPI 生效（每 %.0fs 重试，最长 %dh）...",
        _POLL_INTERVAL, _MAX_HOURS,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _MAX_HOURS * 3600
    while loop.time() < deadline:
        if settings.feishu_allowlist_pending.strip() != "1":
            logger.info("白名单已由首个私聊自动绑定，退出后台解析。")
            return
        try:
            creator = await fetch_creator_open_id(app_id, app_secret)
        except Exception as e:
            logger.debug("创建者解析未就绪：%s", e)
            await asyncio.sleep(_POLL_INTERVAL)
            continue
        settings.allowed_users = creator
        _upsert_env("ALLOWED_USERS", creator)
        settings.feishu_allowlist_pending = ""
        _upsert_env("FEISHU_ALLOWLIST_PENDING", "")
        logger.info("个人用白名单已自动绑定创建者：%s", creator)
        await _notify_creator(
            app_id, app_secret, creator,
            "✅ MyCodex 个人用白名单已自动配置完成，现在可以直接发消息使用了。",
        )
        return
    logger.error("个人用白名单自动绑定超过 %dh 未成功；下次重启服务会继续重试。", _MAX_HOURS)
