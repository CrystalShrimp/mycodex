from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Feishu
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""

    # 企业微信（智能机器人长连接）
    wecom_bot_id: str = ""
    wecom_secret: str = ""

    # Codex CLI
    codex_cli_path: str = "codex"
    codex_default_model: str = ""

    # Workspace
    default_workspace: str = "D:\\projects"

    # Approval
    approval_timeout: int = 600           # Model selection card timeout (10 min)
    tool_approval_timeout: int = 1800     # Tool execution card timeout (30 min)
    tool_approval_warn_seconds: int = 300 # Warn 5 min before tool approval expires
    approval_mode: str = "m"  # h=严格(read-only沙箱) m=平衡(workspace-write) l=全自动(无沙箱)

    # Access control
    allowed_users: str = ""
    # allowed_mode: creator=仅创建人 | org=企业全员 | groups=指定群成员 | list=指定人员
    # 留空 = 旧行为（allowed_users 空=全员，非空=名单）
    allowed_mode: str = ""
    allowed_creator: str = ""
    allowed_group_ids: str = ""

    # Audit
    audit_log_path: str = "./logs/audit.log"

    # Context monitoring
    context_warn_percent: int = 80       # warn when context usage exceeds this %
    context_critical_percent: int = 95   # critical threshold, suggest /new

    # Server
    host: str = "0.0.0.0"
    port: int = 8080
    # 单实例锁端口 — 与同机其他 MyCodex 系部署（如 mycodex 原项目）必须不同，
    # 否则两个服务互相误判"已在运行"而拒绝启动。
    instance_lock_port: int = 48921

    # 通道选择：空 = 自动探测（有 FEISHU_APP_ID 启飞书、有 WECOM_BOT_ID
    # 启企微）；显式逗号列表 = 只启列出的（feishu,wecom）
    channels: str = ""

    def get_enabled_channels(self) -> list[str]:
        raw = self.channels.strip().lower()
        if raw:
            return [c.strip() for c in raw.split(",") if c.strip() in ("feishu", "wecom")]
        out = []
        if self.feishu_app_id and self.feishu_app_secret:
            out.append("feishu")
        if self.wecom_bot_id and self.wecom_secret:
            out.append("wecom")
        return out

    def get_allowed_users(self) -> list[str]:
        return [u.strip() for u in self.allowed_users.split(",") if u.strip()]

    def get_allowed_users_for(self, platform: str) -> list[str]:
        """按平台过滤白名单：无前缀=飞书（兼容旧配置），wecom:xxx=企业微信。

        注意：若原始名单非空但某平台的过滤结果为空，表示该平台无人被授权
        （调用方需区分"原始名单为空=全员放行"与"过滤后为空=全拒"）。
        """
        out: list[str] = []
        for u in self.get_allowed_users():
            if u.startswith("wecom:"):
                if platform == "wecom":
                    out.append(u[len("wecom:"):])
            elif platform != "wecom":
                out.append(u)
        return out

    def get_allowed_mode(self) -> str:
        mode = self.allowed_mode.strip().lower()
        if mode:
            return mode
        # 兼容旧配置：没写 mode 时按 ALLOWED_USERS 推断
        return "list" if self.allowed_users.strip() else "org"

    def get_allowed_group_ids(self) -> list[str]:
        return [g.strip() for g in self.allowed_group_ids.split(",") if g.strip()]

    def get_default_workspace(self) -> str:
        """Return the configured workspace, or the process cwd when unset."""
        configured = self.default_workspace.strip()
        path = Path(configured).expanduser() if configured else Path.cwd()
        return str(path.resolve())

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
