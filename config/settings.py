from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Feishu
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""

    # Claude Code CLI
    claude_cli_path: str = "claude"
    claude_default_model: str = "sonnet"
    claude_data_dir: str = ""

    # Workspace
    default_workspace: str = "D:\\projects"

    # Approval
    approval_timeout: int = 600           # Model selection card timeout (10 min)
    tool_approval_timeout: int = 1800     # Tool execution card timeout (30 min)
    tool_approval_warn_seconds: int = 300 # Warn 5 min before tool approval expires
    approval_mode: str = "m"  # h=高风险(全审批) m=中(高风险审批) l=低风险(全自动放行)
    approval_rules_path: str = "./config/approval_rules.json"

    # Access control
    allowed_users: str = ""

    # Audit
    audit_log_path: str = "./logs/audit.log"

    # Context monitoring
    context_warn_percent: int = 80       # warn when context usage exceeds this %
    context_critical_percent: int = 95   # critical threshold, suggest /new

    # Compact feature (summarize conversation and continue in new session)
    compact_enabled: bool = True

    # Server
    host: str = "0.0.0.0"
    port: int = 8080

    def get_allowed_users(self) -> list[str]:
        return [u.strip() for u in self.allowed_users.split(",") if u.strip()]

    def get_default_workspace(self) -> str:
        """Return the configured workspace, or the process cwd when unset."""
        configured = self.default_workspace.strip()
        path = Path(configured).expanduser() if configured else Path.cwd()
        return str(path.resolve())

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
