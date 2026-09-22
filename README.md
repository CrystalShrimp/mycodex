# MyCodex

把本地 Codex CLI 接入飞书的协作控制层。用户在飞书中发起任务、查看进度，任务实际由本机已登录 ChatGPT 账号的 Codex CLI 执行（`~/.codex/auth.json`，无需 API Key）。MyCodex 负责消息接入、任务调度、配置管理和结果展示。

## 核心能力

- **飞书发起任务，进度实时回流**：同一张飞书卡片增量刷新工具调用（命令执行/文件修改）、阶段性输出和 token 用量，任务完成后切为最终结果，避免中间消息刷屏；
- **终端与飞书双向会话延续**：本机终端和飞书共享 Codex 原生会话（`~/.codex/sessions`），离开工位在飞书接手，回到工位用 `codex resume` 继续，上下文不丢失；
- **三档执行模式（沙箱分级）**：`h` 严格（只读沙箱，禁止写入）/ `m` 平衡（写入限制在工作区内）/ `l` 全自动（无沙箱放行）；
- **多用户多工作区隔离**：每个飞书用户分别维护自己的工作区、模型、推理强度、执行模式和会话；
- **模型与推理强度即时切换**：模型来自本机 codex 登录态（`gpt-5.6-terra` / `gpt-5.6-luna` / `gpt-5.5`），飞书里 `/model`、`/effort` 即时切换。

## 快速开始

**Windows**
1. **前置**：终端运行 `codex login` 完成 ChatGPT 账号登录（键入 `codex` 能启动即具备条件）；
2. **首次配置飞书机器人**：进入 `auto_feishu/` 目录运行 `setup.cmd`，按提示完成一次飞书账号登录即可（详见 [auto_feishu/README.md](auto_feishu/README.md)）；
3. **启动服务**：双击项目根目录的 `MyCodex.bat`，任务栏出现托盘图标即表示启动成功；
4. **在飞书中发消息**给机器人，按引导完成模型、推理强度和执行模式配置后即可使用。

代码或配置更新后，使用 `MyCodex-Restart.bat` 重启。

**macOS**
```bash
bash setup-mac.sh          # 一次性安装：uv 依赖、.env 模板、codex 检查
open MyCodex.command       # 双击启动（停旧→后台起服务→健康检查）
# 可选：
bash scripts/setup_autostart_mac.sh                    # 开机自启 (launchd)
.venv/bin/pip install rumps && .venv/bin/python scripts/menubar.py   # 菜单栏
```

健康检查地址：`http://127.0.0.1:8090/health`。

## 主要指令（飞书中发送）

| 分类 | 指令 | 用途 |
|---|---|---|
| 工作区 | `/cd`、`/pwd`、`/file` | 切换/查看工作区，发送工作区文件 |
| 会话 | `/new`、`/stop`、`/continue`、`/resume <id>`、`/session`、`/clean` | 创建/中断/继续/恢复/列出选择/清理会话 |
| 配置 | `/model`、`/effort`、`/mode`、`/reset` | Codex 模型、推理强度、执行模式 |
| 工具 | `/status`、`/mem`、`/notes`、`/sh`、`/help` | 状态、记忆（AGENTS.md）、笔记、Shell、帮助 |

完整指令说明见 [doc/input.md 附录 A](doc/input.md)。

## 文档导航

| 文档 | 面向读者 | 内容 |
|---|---|---|
| [doc/input.md](doc/input.md) | 产品 / 研究 | 项目定位、核心能力、使用流程（部分内容仍为 Claude 时代版本，待更新） |
| [CLAUDEME.md](CLAUDEME.md) | 部署 Agent | 单机部署工程指南（部分内容仍为 Claude 时代版本，待更新） |
| [auto_feishu/README.md](auto_feishu/README.md) | 客户 | 飞书一键配置脚本的使用与失败恢复 |

## 目录结构

```
app/            后端 FastAPI 服务（事件分发、卡片、Codex CLI 调度）
auto_feishu/    飞书一键配置子模块（TypeScript + Playwright）
config/         服务设置（settings.py）
doc/            产品文档
examples/       `.env` 模板
logs/           运行日志（mycodex.log、audit.log）和会话状态
scripts/        托盘程序、重启脚本、开机自启等
```

## 配置入口

- **`.env`**：从 `examples/.env.example` 复制。包含飞书凭据、`ALLOWED_USERS` 访问白名单、默认工作区、服务端口（8090）等；
- **`icon.png` / `icon.jpg` / `icon.ico`**（可选）：放到项目根目录可自定义托盘图标，缺失时使用系统默认图标。

## 路线图

- **阶段二（未实施）**：接入 `codex app-server` 协议，恢复飞书逐工具审批卡片（沙箱分级之外的细粒度审批）。
