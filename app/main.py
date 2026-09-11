from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

from config.settings import settings
from app.feishu.client import feishu_client
from app.feishu.ws import FeishuWsClient, create_ws_client
from app.feishu import events
from logging.handlers import RotatingFileHandler

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            "logs/myclaw.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
import socket
import sys

logger = logging.getLogger("myclaw.main")

_instance_lock_socket: socket.socket | None = None

def ensure_single_instance(lock_port: int = 48921) -> None:
    global _instance_lock_socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", lock_port))
        sock.listen(1)
        _instance_lock_socket = sock
    except OSError:
        logger.warning(
            "⚠️ [SingleInstance] MyClaw 已在后台运行中 (端口 %d 被占用)，无法重复启动。",
            lock_port,
        )
        sys.exit(0)

# Only acquire the single-instance lock when run as the entrypoint
# (`python -m app.main`). When uvicorn re-imports this module via the
# string "app.main:app" path, top-level code re-executes — without this
# guard, ensure_single_instance() would run twice in the same process,
# the second bind would fail, and sys.exit(0) would silently kill the
# uvicorn worker before it could bind its port.
if __name__ == "__main__":
    ensure_single_instance(settings.instance_lock_port)

# Build event dispatcher
event_handler = (
    EventDispatcherHandler.builder(
        encrypt_key=settings.feishu_encrypt_key,
        verification_token=settings.feishu_verification_token,
    )
    .register_p2_im_message_receive_v1(events.on_message_receive)
    .register_p2_card_action_trigger(events.on_card_action)
    .build()
)

ws_client: FeishuWsClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ws_client
    workspace = Path(settings.get_default_workspace())
    if workspace.exists() and not workspace.is_dir():
        raise NotADirectoryError(
            f"DEFAULT_WORKSPACE exists but is not a directory: {workspace}"
        )
    workspace.mkdir(parents=True, exist_ok=True)

    logger.info("myclaw starting...")
    logger.info("Default workspace: %s", workspace)
    logger.info("Allowed users: %s", settings.get_allowed_users() or "(all)")
    logger.info("Codex CLI: %s", settings.codex_cli_path)

    # Start Feishu WebSocket long-connection
    ws_client = create_ws_client(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        event_handler=event_handler,
    )
    ws_client.start_async()
    logger.info("Feishu WS client connecting...")

    # 群聊"仅 @ 响应"门槛需要机器人自身 open_id 来比对 mentions
    try:
        if await feishu_client.fetch_bot_open_id():
            logger.info("Group messages gated on @bot mention")
        else:
            logger.warning(
                "Bot open_id unavailable; group @-gate falls back to any-mention check"
            )
    except Exception as e:
        logger.warning("Failed to fetch bot open_id: %s", e)

    # Models are discovered from the local codex login (models_cache.json).
    from app.profiles import discover_models
    models = discover_models()
    if not models:
        logger.error("No codex models discovered (check ~/.codex/models_cache.json)")
    else:
        logger.info("Available codex models: %s", list(models.keys()))
    yield

    await feishu_client.close()
    logger.info("myclaw stopped.")


app = FastAPI(
    title="myclaw",
    description="Feishu Bot backed by Codex CLI",
    version="0.3.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    info = {"status": "ok", "ws_connected": ws_client._conn is not None if ws_client else False}
    if ws_client:
        info["ws_diagnostics"] = {
            "msg_count": ws_client._msg_count,
            "last_msg_time": ws_client._last_msg_time,
            "last_msg_type": ws_client._last_msg_type,
            "last_msg_error": ws_client._last_msg_error,
            "dispatch_count": ws_client._dispatch_count,
        }
    from app.agent.cli_loop import codex_cli_loop
    info["cli_last_error"] = codex_cli_loop._last_error or "(none)"
    return info


if __name__ == "__main__":
    import sys

    dev_mode = "--dev" in sys.argv
    if dev_mode:
        logger.info("DEV mode: hot-reload enabled")
        uvicorn.run(
            "app.main:app",
            host=settings.host,
            port=settings.port,
            reload=True,
            reload_includes=["*.py"],
            reload_excludes=["logs/*", "logs/audit.log", "logs/myclaw.log", ".env", "*.log"],
        )
    else:
        uvicorn.run(
            "app.main:app",
            host=settings.host,
            port=settings.port,
            reload=False,
        )
