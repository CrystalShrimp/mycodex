from pathlib import Path
import sys

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

    # Workspace（Codex 默认代码工程目录；未配置时跨平台智能回退）
    default_workspace: str = ""

    # Approval
    approval_timeout: int = 600           # Model selection card timeout (10 min)
    tool_approval_timeout: int = 1800     # Tool execution card timeout (30 min)
    tool_approval_warn_seconds: int = 300 # Warn 5 min before tool approval expires
    approval_mode: str = "m"  # h=严格(read-only沙箱) m=平衡(workspace-write) l=全自动(无沙箱)
    approval_rules_path: str = "./config/approval_rules.json"

    # Access control
    # 飞书访问白名单（留空 = 全员放行，非空 = 仅名单内 open_id 可用）
    allowed_users: str = ""
    # 企业微信访问白名单（留空 = 全员放行，非空 = 仅名单内 userid 可用，无须 wecom: 前缀）
    wecom_allowed_users: str = ""
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
    port: int = 8090
    # 单实例锁端口 — 与同机其他 MyCodex 系部署（如 mycodex 原项目）必须不同，
    # 否则两个服务互相误判"已在运行"而拒绝启动。
    instance_lock_port: int = 48921

    # 通道选择：空 = 自动探测（有 FEISHU_APP_ID 启飞书、有 WECOM_BOT_ID
    # 启企微）；显式逗号列表 = 只启列出的（feishu,wecom）
    channels: str = ""
    myclaw_channels: str = ""

    def get_enabled_channels(self) -> list[str]:
        raw = (self.channels or self.myclaw_channels).strip().lower()
        if raw:
            return [c.strip() for c in raw.split(",") if c.strip() in ("feishu", "wecom")]
        out = []
        if self.feishu_app_id and self.feishu_app_secret:
            out.append("feishu")
        if self.wecom_bot_id and self.wecom_secret:
            out.append("wecom")
        return out

    def get_allowed_users(self) -> list[str]:
        """返回飞书有效白名单用户列表（兼容原有调用）。"""
        return self.get_feishu_allowed_users()

    def get_feishu_allowed_users(self) -> list[str]:
        """获取飞书白名单用户 open_id 列表（自动过滤掉可能残留的 wecom: 项）。"""
        return [
            u.strip() for u in self.allowed_users.split(",")
            if u.strip() and not u.strip().startswith("wecom:")
        ]

    def get_wecom_allowed_users(self) -> list[str]:
        """获取企业微信白名单 userid 列表。

        优先读取 WECOM_ALLOWED_USERS；若未配置该键，向下兼容从 ALLOWED_USERS 中解析 wecom: 前缀项。
        """
        raw = self.wecom_allowed_users.strip()
        if raw:
            res = []
            for u in raw.split(","):
                u = u.strip()
                if u.startswith("wecom:"):
                    u = u[len("wecom:"):].strip()
                if u and u not in res:
                    res.append(u)
            return res

        # 向下兼容旧配置：ALLOWED_USERS 中若有 wecom: 项
        compat = []
        for u in self.allowed_users.split(","):
            u = u.strip()
            if u.startswith("wecom:"):
                uid = u[len("wecom:"):].strip()
                if uid and uid not in compat:
                    compat.append(uid)
        return compat

    def get_allowed_users_for(self, platform: str) -> list[str]:
        """按平台获取对应的独立白名单列表。"""
        if platform == "wecom":
            return self.get_wecom_allowed_users()
        return self.get_feishu_allowed_users()

    def get_allowed_mode(self) -> str:
        mode = self.allowed_mode.strip().lower()
        if mode:
            return mode
        # 兼容旧配置：没写 mode 时按 ALLOWED_USERS 推断
        return "list" if self.allowed_users.strip() else "org"

    def get_allowed_group_ids(self) -> list[str]:
        return [g.strip() for g in self.allowed_group_ids.split(",") if g.strip()]

    def get_default_workspace(self) -> str:
        """返回已配置的工作空间路径，若未设置或不存在则优雅跨平台回退。"""
        configured = self.default_workspace.strip()
        if configured:
            path = Path(configured).expanduser()
            try:
                path.mkdir(parents=True, exist_ok=True)
                return str(path.resolve())
            except Exception:
                if path.exists():
                    return str(path.resolve())

        # 未配置或指定路径不可用时，优雅回退到系统可用目录
        if sys.platform != "win32":
            candidate = Path.home() / "projects"
            candidate.mkdir(parents=True, exist_ok=True)
            return str(candidate.resolve())
        else:
            preferred = Path("D:\\projects") if Path("D:\\").exists() else Path("C:\\projects")
            try:
                preferred.mkdir(parents=True, exist_ok=True)
                return str(preferred.resolve())
            except Exception:
                pass

        return str(Path.cwd().resolve())

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
