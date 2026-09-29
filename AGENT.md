# MyCodex 本地 Agent 配置与运维指南 (AGENT.md)

本文档面向运行在用户本地电脑上的 AI 编程助手（Codex CLI、Claude Code、Cursor 等 Agent）。当用户向你咨询如何配置、启动、重跑或排查 **MyCodex** 时，请严格以本文档及源码为准提供协助。

---

## 一、项目定位与核心原理

**MyCodex** 是将本地电脑上的 **OpenAI Codex CLI (`@openai/codex`)** 桥接到 **飞书 (Feishu)** 或 **企业微信 (WeCom)** 的本地守护服务：
- **零公网暴露**：采用飞书/企微官方 **WebSocket 长连接模式** 收发消息与卡片回调，无需公网 IP、无需内网穿透或反向代理。
- **终端与 IM 会话互通**：直接读取本机 Codex CLI 的原生会话存储（默认 `~/.codex/sessions`），支持在手机飞书/企微上用 `/session`、`/resume`、`/continue` 无缝续接电脑终端的编程会话，也支持随时通过 `/cd` 切换目标工程目录。

---

## 二、跨平台启动与配置入口

用户可通过桌面启动器或命令行进入 **四步交互式配置向导 (`scripts/setup_wizard.py`)**：

### 1. 首次配置（Setup 向导）
- **Windows**：双击运行 `launcher_win\MyCodex-Setup.bat`（或根目录 `setup.bat`）
- **macOS**：双击运行 `launcher_macos/MyCodex-Setup.command`
- **命令行直跑**：
  ```bash
  python scripts/setup_wizard.py
  ```

### 2. 日常启动服务
- **Windows**：双击运行 `launcher_win\MyCodex.bat`（或根目录 `start.bat`）
- **macOS**：双击运行 `launcher_macos/MyCodex.command`
- **命令行直跑**：
  ```bash
  python -m app.main
  ```

---

## 三、Setup 四步配置全流程解析

配置向导（[`scripts/setup_wizard.py`](scripts/setup_wizard.py)）按顺序执行以下 4 个核心步骤，并在检测到已完成配置时展示可视化状态面板：

### Step 1/4：运行环境自检与依赖 (`check_environment`)
1. **Node.js (v20+)**：用于运行 Codex CLI 及飞书/企微浏览器自动化配置脚本（`auto_feishu` / `auto_wecom`）。
2. **Codex CLI (`codex`)**：若本机未安装，向导会提示并自动通过 `npm install -g @openai/codex` 安装（国内网络自动启用 `npmmirror` 镜像加速）。
3. **Playwright Chromium**：首次执行飞书/企微自动化配置前，自动安装 Chromium 驱动并清理残留的 `__dirlock`。

### Step 2/4：初始运行目录与历史会话目录确认 (`confirm_directories`)
向导会引导确认并写入根目录 `.env` 中的两个核心路径配置：
1. **初始运行目录 (`DEFAULT_WORKSPACE`)**
   - **定位**：“初始任务临时在此运行，后续可用 `/cd` 命令切换至目标目录”。
   - **默认值**：默认设置为**当前 MyCodex 项目所在目录**。
   - **运行机制**：仅在用户尚未创建会话或未使用 `/cd` 切换目录时作为初始临时运行目录。一旦用户在飞书/企微中通过 `/cd` 切换到目标业务工程目录，后续对话及 `/new`（在当前工作区开启新会话）均在目标目录执行，不会跳回 `DEFAULT_WORKSPACE`。
2. **Codex 历史会话目录 (`CODEX_SESSION_DIR`)**
   - **定位**：“读取电脑端的历史会话”。
   - **默认值**：自动探测电脑端 Codex CLI 历史会话存储目录（优先读取 `$CODEX_HOME/sessions`，默认 `~/.codex/sessions`）。
   - **运行机制**：供 `/cd`（无参弹出历史项目卡片）、`/session`、`/resume`、`/continue` 枚举并恢复电脑端终端的历史会话（解析 `rollout-*.jsonl` 中的 `session_meta.payload.cwd` 与对话记录）。

### Step 3/4：Codex 账号认证与初始运行配置 (`check_or_setup_models`)
- 自动检测本机是否存在有效的 ChatGPT 授权凭证（`~/.codex/auth.json`）；若未检测到则引导调用 `codex login`。
- 交互式设置三项初始运行偏好并写入 `config/global_preferences.json`：**Model（默认模型）**、**Effort（思考力度：`low`/`medium`/`high`/`xhigh`/`max`）**、**Mode（权限审批模式：`h`/`m`/`l`）**。

### Step 4/4：配置中心（菜单选项 `1` - `7`）
- **选项 `1` — 配置飞书 - 个人：仅创建者可用**
  - 自动创建/复用飞书自建应用（支持自定义应用名称，默认 `mycodex`），导入权限（含 `application:application:self_manage`）、开启长连接并发布版本，自动查询应用创建者 `open_id` 写入 `.env` 的 `ALLOWED_USERS`。
- **选项 `2` — 配置飞书 - 公用：全部成员可用**
  - 全自动完成飞书应用配置与发版，默认 `ALLOWED_USERS` 留空对全员开放，并询问是否立即导入指定飞书群成员白名单。
- **选项 `3` — `└─ 一键授权飞书群成员：基于2，限制仅特定群成员可用`**
  - 调用 `scripts/import_feishu_group.py`，支持选择 **重置群成员名单（覆盖）** 或 **添加新的群成员名单（追加合并）**。
- **选项 `4` — 配置企业微信**
  - 自动配置企业微信智能机器人长连接（写入 `WECOM_BOT_ID` 与 `WECOM_SECRET`）。
- **选项 `5` — Codex 认证与初始运行配置（登录切换 / Model / Effort / Mode）**
- **选项 `6` — 配置开机自启（每次开机自动静默后台运行）**
- **选项 `7` — 查看/重置初始化配置（工作空间/运行环境/Codex 认证与初始偏好）**

---

## 四、关键文件说明与重置方法

| 路径 | 作用说明 |
| :--- | :--- |
| `.env` | 核心环境变量配置文件（飞书 `FEISHU_APP_ID`/`FEISHU_APP_SECRET`、目录配置、访问控制白名单等）。 |
| `~/.codex/auth.json` | Codex CLI 的本机 ChatGPT 登录凭证。 |
| `auto_feishu/feishu-app-result.json` | 飞书自动化脚本的断点续跑状态记录。 |
| `auto_feishu/artifacts/` | Playwright 飞书浏览器登录态缓存（`feishu-user-data`）及排障截图/HTML 快照。 |
| `.sessions/` & `.preferences/` | IM 会话状态绑定与用户在 IM 内的模型/推理强度/审批模式偏好缓存。 |

### 如何彻底清空用户数据并从头运行 Setup？
若用户希望清除历史配置、模拟全新环境从头运行 Setup，可清理以下文件（保留 `auto_feishu/artifacts/feishu-user-data` 可免去重新扫飞书二维码）：
- **Windows (PowerShell)**：
  ```powershell
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue .env, auto_feishu\feishu-app-result.json, auto_feishu\artifacts, auto_feishu\logs, .sessions, .preferences
  ```
- **macOS / Linux**：
  ```bash
  rm -rf .env auto_feishu/feishu-app-result.json auto_feishu/artifacts auto_feishu/logs .sessions .preferences
  ```

---

## 五、IM 端常用命令速查

配置完成并启动服务后，用户可在飞书/企业微信中向机器人发送以下指令：
- `/cd`：弹出交互卡片，从电脑端 Codex 历史项目列表中一键切换工作区；或发送 `/cd <绝对路径>` 切换到指定目录。
- `/pwd`：查看当前会话所在的工作目录。
- `/session`：列出当前工作区下的电脑端历史 Thread 会话并点击恢复。
- `/continue`：直接续接当前工作区最近一次的 Codex 会话。
- `/new`：在当前工作区（继承 `/cd` 后的目录）开启全新的对话会话。
- `/model`：切换 Codex 模型与推理强度（Effort: low / medium / high）。
- `/mode`：切换安全沙箱与审批模式（`h` 只读沙箱 / `m` 工作区读写 / `l` 全自动无沙箱）。
- `/file`：浏览并下载当前工作区中的文件到飞书/企微。
- `/status`：查看当前工作区、关联 Thread ID、模型配置与运行状态。
