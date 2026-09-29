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

### Step 3/4：Codex 账号认证 (`ensure_codex_auth`)
- 自动检测本机是否存在有效的 ChatGPT 授权凭证（`~/.codex/auth.json`）。
- 若未检测到凭证，向导自动调用 `codex login` 拉起浏览器完成 ChatGPT 账号登录授权，无需手动配置 API Key。

### Step 4/4：配置中心（飞书 / 企业微信自动化接入与白名单）
在配置中心菜单中，用户可选择：
- **选项 `1` — 飞书机器人 · 个人用模式（推荐个人开发者）**
  - 启动 Playwright 浏览器打开飞书开放平台，引导用户扫码登录；
  - 支持选择已有企业自建应用，或选 `0` **创建全新应用**（支持自定义应用名称，直接回车默认为 `mycodex`）；
  - 全自动完成：获取凭证写入 `.env` $\rightarrow$ 批量导入权限（含消息收发及 `application:application:self_manage`） $\rightarrow$ 启用机器人能力 $\rightarrow$ 后台拉起本地服务并建立 WebSocket 长连接以通过飞书事件订阅校验（订阅 `im.message.receive_v1` 与 `card.action.trigger`） $\rightarrow$ 自动创建并发布版本；
  - **个人白名单自动收尾**：发版生效后，自动调用飞书 OpenAPI 查询应用创建者本人的 `open_id`，并将其写入 `.env` 的 `ALLOWED_USERS`（配合 `ALLOWED_MODE=creator`），确保仅本人可调用该机器人。
- **选项 `2` — 飞书机器人 · 全员 / 群聊模式（团队共享）**
  - 同样全自动完成飞书应用配置与发版，并支持配置为企业全员可用（`ALLOWED_MODE=org`）或拉取指定群聊成员白名单（`scripts/import_feishu_group.py`）。
- **选项 `3` — 企业微信智能机器人接入 (`auto_wecom`)**
  - 自动配置企业微信智能机器人长连接（写入 `WECOM_BOT_ID` 与 `WECOM_SECRET`）。

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
