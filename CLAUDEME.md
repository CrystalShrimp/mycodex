# MyCodex 配置指南（给 Agent 看）

> 这份文档面向**帮客户部署 MyCodex 的 Agent**（人或不人）。读完应能：
> 1. 在 10 分钟内完成一台 Windows 机器的部署
> 2. 用 **Auto feishu** 一键完成飞书开放平台配置（5 分钟，客户只登录一次）
> 3. 处理 90% 的个性化需求（快捷命令、权限、自启动、模型切换）

如果你是**客户**而不是 Agent，请看 `doc/report.md`（产品介绍和魔法指令用法）。

---

## 1. MyCodex 是什么（30 秒版）

把客户本机已装好的 OpenAI Codex CLI 包装成飞书机器人。飞书消息 → MyCodex 调本地 `codex exec` → 流式回飞书卡片，写文件/跑命令前弹审批卡片。**代码、API Key、对话历史全部留在客户本机**；模型调用走本机 ChatGPT 登录态（`codex login`），不配置任何供应商 API Key。

---

## 2. 前置条件检查

部署前逐项检查客户机：

| 项 | 检查命令 | 期望 | 缺失处理 |
|---|---|---|---|
| Node.js | `node --version` | **v20+**（Auto feishu 要求） | https://nodejs.org/ 下载 LTS |
| Codex CLI | `codex --version` | 已安装 | `npm i -g @openai/codex` |
| Codex 登录 | `codex login status` | `Logged in` | 终端执行 `codex login`（浏览器 ChatGPT 授权） |
| Python | `python --version` | 3.11+ | https://www.python.org/ |
| uv | `uv --version` | 已安装 | `pip install uv` 或 PowerShell `irm https://astral.sh/uv/install.ps1 \| iex` |
| 飞书账号 | 能登录 open.feishu.cn | 已是企业管理员或有自建应用权限 | 联系客户企业管理员开通 |

Windows 上 codex CLI 通过 `shutil.which()` 解析，会自动找到 `codex.CMD`，**不需要手动配完整路径**。

---

## 3. 一键部署流程

```bash
# 1. 把项目放到目标目录（假设 D:\ForRunning\ForDev\mycodex）
cd D:\ForRunning\ForDev\mycodex

# 2. 安装 Python 依赖（会自动建 .venv）
uv sync

# 3. 复制环境变量模板
cp examples/.env.example .env

# 4. 确认 Codex 已登录（模型走本机 ChatGPT 登录态，无需任何 API Key）—— 详见 §5
codex login status    # 应显示 Logged in；否则执行 codex login

# 5. 【推荐】用 Auto feishu 一键完成飞书配置（自动写 .env 的飞书字段）—— 详见 §6.1
cd auto_feishu && setup.cmd

# 6. 启动本地服务
cd ..
uv run python -m app.main
# 看到 "Feishu WS client connecting..." 即成功
```

启动后日志会打印：默认 workspace、允许的用户列表、Codex CLI 路径、WS 连接状态。

**验证**：浏览器打开 `http://localhost:8090/health`，应返回 `{"status":"ok","ws_connected":true,...}`。

> 如果客户已经手工在飞书后台配过应用，可以跳过第 5 步，直接编辑 `.env` 填入飞书凭据（§4.1）。

---

## 4. `.env` 字段详解

模板见 `examples/.env.example`。**必填项**标 ★，修改后**必须重启**才生效（除非另有说明）。

### 4.1 飞书应用凭据（Auto feishu 会自动写入这几项）

| 字段 | 默认 | 说明 |
|---|---|---|
| ★ `FEISHU_APP_ID` | `""` | 飞书自建应用 App ID（`cli_xxx`） |
| ★ `FEISHU_APP_SECRET` | `""` | 飞书自建应用 App Secret |
| ★ `FEISHU_VERIFICATION_TOKEN` | `""` | 事件订阅里的 Verification Token |
| `FEISHU_ENCRYPT_KEY` | `""` | 事件加密 Key（如启用了 Encrypt Mode） |

> 用 Auto feishu 时这 4 项会自动从飞书后台抓取并写入 `.env`，不用手工填。

### 4.2 Codex CLI

| 字段 | 默认 | 说明 |
|---|---|---|
| `CODEX_CLI_PATH` | `"codex"` | CLI 名称或绝对路径，`shutil.which` 解析；Windows 自动找 `codex.CMD` |
| `CODEX_DEFAULT_MODEL` | `""` | 兜底模型 slug（用户未选时用，如 `gpt-5.6-luna`） |

> 认证完全依赖本机 `codex login` 的 ChatGPT 登录态（`~/.codex/auth.json`），没有 API Key 类配置。

### 4.3 工作区

| 字段 | 默认 | 说明 |
|---|---|---|
| `DEFAULT_WORKSPACE` | `D:\projects` | 兜底工作区；客户 `/cd` 后会自动覆写此项 |

### 4.4 审批

| 字段 | 默认 | 说明 |
|---|---|---|
| `APPROVAL_TIMEOUT` | `600` | 模型选择卡片超时（秒，10 分钟） |
| `TOOL_APPROVAL_TIMEOUT` | `1800` | 工具审批总超时（秒,30 分钟） |
| `TOOL_APPROVAL_WARN_SECONDS` | `300` | 超时前预留催办窗口（秒，5 分钟） |
| `APPROVAL_MODE` | `"m"` | 默认审批模式：`h`=严格（只读沙箱） / `m`=平衡（写入限工作区） / `l`=全自动（无沙箱） |

> 用户在飞书里 `/mode X` 后会写到 `.preferences/`，**覆盖**这个默认值。

### 4.5 单实例锁

| 字段 | 默认 | 说明 |
|---|---|---|
| `INSTANCE_LOCK_PORT` | `48922` | 单实例锁端口（与同机 mycodex 原项目错开） |

### 4.6 访问控制

| 字段 | 默认 | 说明 |
|---|---|---|
| `ALLOWED_USERS` | `""` | 允许的 open_id 白名单，逗号分隔；**空 = 全部允许**（生产环境强烈建议填） |

获取 open_id 的方式：让用户先在飞书发任意消息。若其不在白名单内，机器人会直接回复一条包含其 Open ID 和加白指引的消息（自助式）；也可从 `mycodex.log` 里的 `From ou_xxx: ...` 日志获取。

### 4.7 审计 / 服务

| 字段 | 默认 | 说明 |
|---|---|---|
| `AUDIT_LOG_PATH` | `./logs/audit.log` | 审计日志位置（JSON-lines） |
| `HOST` | `0.0.0.0` | FastAPI 监听地址 |
| `PORT` | `8090` | FastAPI 监听端口（与同机 mycodex 的 8080 错开） |

---

## 5. 模型与推理档位（Codex）

模型不需要任何配置文件：可用模型列表自动从 codex 本机缓存（`~/.codex/models_cache.json`，尊重 `CODEX_HOME`）读取；缓存缺失时回退到 `app/profiles.py` 内置的静态列表（gpt-5.6-terra 旗舰 / gpt-5.6-luna 均衡 / gpt-5.5 轻量）。

### 5.1 登录与验证

```bash
codex login status    # 应显示 Logged in
codex login           # 未登录时执行，浏览器 ChatGPT 授权
```

`app/profiles.py` 的 `test_codex_auth()` 通过 `codex login status` 验证登录态（不消耗额度），`/status` 里会展示。

### 5.2 切换模型 / 档位

- 飞书发 `/model`（或 `/model gpt-5.6-luna`）：弹模型选择卡片 / 直接切换
- 飞书发 `/level` 或 `/effort`：切推理强度 `low / medium / high / xhigh / max`
- 兜底模型：`.env` 的 `CODEX_DEFAULT_MODEL`（留空用内置列表第一个）

### 5.3 历史遗留说明

旧版本的 `config/settings_<name>.json` 供应商 profile、`config/active_profile` 标记和 `/provider` 命令已移除——模型统一走本机 ChatGPT 登录态，不再注入任何供应商 env / API Key。

---

## 6. 飞书开放平台配置

提供两种方式，**强烈推荐用 Auto feishu 自动化**（5 分钟一键完成，客户只登录一次）；手工方案作为兜底。

### 6.1 方式 A：Auto feishu 一键自动化（推荐）

项目自带 `auto_feishu/` 目录，是一个 Node.js + Playwright 工具，专门做飞书开放平台的一键交付。

#### 6.1.1 Auto feishu 会做什么

Auto feishu 启动 Playwright 浏览器，模拟人工点击完成下面全部步骤：

1. 创建或复用飞书企业自建应用
2. 启用机器人能力
3. 导入 `feishu-permissions.json` 的全部权限（22 tenant + 3 user scope）
4. 抓取 App ID / App Secret / Verification Token，**只写入项目根目录 `.env`**（不存 Secret 到 result 文件）
5. 配置事件订阅方式为长连接，订阅 `im.message.receive_v1`
6. 配置回调方式为长连接，订阅 `card.action.trigger`
7. 启动本地 MyCodex 服务，或校验已运行（事件订阅要求网关在线）
8. 创建并发布应用版本
9. 写入 `auto_feishu/feishu-app-result.json`（不含 Secret，含 appId、maskedSecret、完成步骤、artifacts 路径）

#### 6.1.2 前置条件

- 客户机 Node.js ≥ 20（`node --version` 检查；Auto feishu 的 `engines.node` 要求）
- 客户的飞书账号能登录 https://open.feishu.cn，且在企业管理员通过的租户内
- 项目根目录已 `uv sync` 安装好 Python 依赖（Auto feishu 会启动 MyCodex 校验健康）
- 端口 8090 未被占用（或修改 `.env` 的 `PORT` + `auto_feishu/config.json` 的 `localServiceUrl` 保持一致）

#### 6.1.3 一键运行

Windows 客户终端：

```bat
cd D:\ForRunning\ForDev\mycodex\auto_feishu
setup.cmd
```

`setup.cmd` 会自动完成：

1. 清理可能影响飞书 API 的代理环境变量（识别 `HTTP_PROXY=127.0.0.1:6984` 并清空）
2. `where node` 检查 Node.js
3. `npm ci --ignore-scripts`（安装锁定依赖，不跑 postinstall）
4. `npx playwright install chromium`（准备 Playwright 浏览器）
5. `npm run feishu:setup`（启动自动化）

客户在弹出的浏览器里**扫码登录飞书一次**，后续无需手工干预，全程 3-5 分钟。

#### 6.1.4 跨平台 / 调试运行

非 Windows 或想看更详细日志：

```bash
cd auto_feishu
npm ci --ignore-scripts
npx playwright install chromium

# 正式跑
npm run feishu:setup

# 调试模式（更详细日志）
npm run feishu:setup:debug

# 仅做观察录制（不修改任何配置）
npm run feishu:observe
```

#### 6.1.5 配置文件 `auto_feishu/config.json`

控制自动化的全部行为，关键字段：

| 字段 | 默认 | 说明 |
|---|---|---|
| `appName` | `"mycodex"` | 飞书应用显示名（按客户品牌改，例如 `"MyCodex 助手"`） |
| `appDescription` | `"用于接入 mycodex 的飞书机器人"` | 应用描述 |
| `appIconPath` | `"./icon.png"` | 应用图标路径（不存在则跳过） |
| `botName` | `"mycodex 助手"` | 机器人显示名 |
| `reuseStartedApp` | `false` | 是否优先复用"已启动"状态的应用 |
| `permissionsImportJsonPath` | `"./feishu-permissions.json"` | 权限 JSON 路径（22+3 scope） |
| `enableGroupMessagePermission` | `true` | 是否启用群消息权限 |
| `enableEventSubscription` | `true` | 是否配置事件订阅 |
| `eventNames` | `["im.message.receive_v1", "card.action.trigger"]` | 订阅的事件 |
| `publishAfterSetup` | `true` | 完成后自动创建版本并发布 |
| `envPath` | `"../.env"` | 凭据写入位置（项目根目录 `.env`） |
| `localServiceUrl` | `"http://127.0.0.1:8090/health"` | 本地服务健康检查 URL |
| `localServiceRootDir` | `".."` | 本地服务根目录（用于启动 MyCodex） |
| `startLocalService` | `true` | 是否自动启动本地服务（事件订阅要求网关在线） |
| `localServiceWaitMs` | `30000` | 本地服务启动等待上限 |
| `resultPath` | `"./feishu-app-result.json"` | 交付结果 JSON（不含 Secret） |
| `screenshotsDir` | `"./artifacts/screenshots"` | 失败时截图保存目录 |
| `htmlDumpDir` | `"./artifacts/html"` | 失败时 HTML 快照目录 |
| `storageStatePath` | `"./artifacts/feishu-storage-state.json"` | 浏览器登录状态保存（含 cookie） |
| `userDataDirPath` | `"./artifacts/feishu-user-data"` | Playwright 持久化用户数据目录 |
| `timeoutMs` | `20000` | 单步操作默认超时 |
| `loginTimeoutMs` | `600000` | 登录超时（10 分钟，留给客户扫码） |

**常见修改**：

| 需求 | 改哪里 |
|---|---|
| 改应用名为客户品牌 | `appName` / `appDescription` / `botName`（注意：仅对**新创建**的应用生效；已创建的应用要手工去飞书后台改） |
| 不自动发布版本（手工审一遍再发） | `publishAfterSetup: false` |
| 不自动启动本地服务（已在外部启动） | `startLocalService: false` |
| 改端口 | 同时改 `.env` 的 `PORT` + `MYCODEX_PORT` + 这里 `localServiceUrl` |

#### 6.1.6 失败恢复与续跑

Auto feishu **可重复运行**，每次会：

- 读 `feishu-app-result.json`，复用已创建的应用（不会重复创建）
- 读 `artifacts/feishu-storage-state.json`，复用上次登录状态（不用重新扫码）
- 读 `.env` 已写入的凭据
- 跳过 `result.json` 里标记 `completed` 的步骤

遇到外部阻挡（验证码、租户审批、组织策略、权限不足、飞书页面改版）时，会输出：

- 失败步骤名（`lastCompletedStep` 之后那一步）
- 错误原因
- 截图路径：`auto_feishu/artifacts/screenshots/<timestamp>-step.png`
- HTML 快照路径：`auto_feishu/artifacts/html/<timestamp>-step.html`

**解除阻挡后重新运行 `setup.cmd` 即可续跑**，不用从头开始。

#### 6.1.7 验证交付完成

看到以下输出即成功：

```text
交付完成：.env 已写入飞书凭据，结果 JSON 未保存 Secret，可在飞书测试机器人。
```

`auto_feishu/feishu-app-result.json` 应满足：

- `status` = `"completed"`
- `lastCompletedStep` = `"创建并发布飞书应用版本"`
- `loginCompleted` / `permissionsImported` / `botEnabled` / `eventSubscriptionConfigured` / `published` 全部 `true`
- `appId` 非空，`maskedSecret` 形如 `a3hl****Cilc`
- `failure` = `null`

随后在飞书向机器人发条消息（如"你好"），确认 `mycodex.log` 出现 `From ou_xxx:` 日志且机器人有回复。

#### 6.1.8 安全约束

- App Secret **只**写入项目根目录 `.env`，**不进** `feishu-app-result.json`、**不进**日志、**不进**截图
- `feishu-app-result.json` 只保留 `maskedSecret`（前 4 + 后 4 字符）
- 浏览器 storage state 保留在 `auto_feishu/artifacts/feishu-storage-state.json`（含登录 cookie）——**别提交到 Git**（项目 `.gitignore` 已默认排除 `artifacts/`）
- 截图如不慎包含 Secret 区域，客户应在交付后清理 `artifacts/screenshots/` 目录
- `feishu-permissions.json` 是社区标准的 22 tenant + 3 user scope，**别随意删减**（删了可能导致缺权限报错）

#### 6.1.9 Auto feishu 失败排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `setup.cmd` 报 Node.js not found | Node.js 未装或版本 < 20 | 装 Node.js LTS（≥ 20） |
| 浏览器没起来 | Playwright Chromium 未就绪 | 手工 `npx playwright install chromium` |
| 卡在登录页 | 代理把 `open.feishu.cn` 走了代理 | 清理 `HTTP_PROXY` 环境变量或代理规则里把 `feishu.cn` 设为直连 |
| 登录后无反应 | 客户账号不是企业管理员、或租户未开通自建应用权限 | 换企业管理员账号或联系租户管理员 |
| 权限导入失败 | 飞书改了 Monaco 编辑器或批量导入入口 | 看 `artifacts/screenshots/` 最新截图定位；或暂时改用 §6.2 手工方案 |
| 事件订阅保存失败 | 本地 MyCodex 服务未在线（事件订阅要求网关先在线） | 先 `uv run python -m app.main` 起服务，再重跑 |
| 发布失败：可用范围不足 | 企业策略要求可用范围必须指定 | 手工去飞书后台「版本管理与发布」补可用范围 |
| `status: "failed"` 反复 | 飞书页面改版 | 跑 `npm run feishu:setup:debug` 拿详细日志，对照 `doc/auto.md` 排查；或暂时改用 §6.2 手工方案 |

### 6.2 方式 B：手工配置（兜底）

如果 Auto feishu 因飞书页面改版、租户策略等失败，可以手工配置。完整步骤见 `scripts/feishu_bot/MANUAL_SETUP.md`（8 步流程，15-20 分钟），简要：

1. https://open.feishu.cn 登录 → 创建企业自建应用
2. **应用功能 → 机器人** → 开启
3. **权限管理 → 批量导入** → 粘贴 `scripts/feishu_bot/openclaw-scopes.json`（22 tenant + 3 user scope）→ 申请开通
4. **凭证与基础信息** → 复制 App ID / App Secret，手工填到 `.env`
5. 启动本地 MyCodex 服务（`uv run python -m app.main`）
6. **事件与回调 → 事件配置** → 选**长连接**模式 → 添加 `im.message.receive_v1`
7. **事件与回调 → 回调配置** → 选**长连接**模式 → 添加 `card.action.trigger`
8. **版本管理与发布** → 创建版本 → 提交（企业内部应用通常自动通过）
9. 飞书里搜机器人名字发消息测试

> **顺序很重要**：第 5 步（启动本地服务）必须早于第 6 步（配置事件订阅长连接），否则飞书会拒绝保存。

> 注意：`scripts/feishu_bot/` 下的 `recorder.py` / `replayer.py` / `diagnose_session.py` / `HANDOFF.md` / `help.md` 是早期自动化尝试的废弃产物（卡在 CSRF token），**已废弃不用**。保留的有用文件只有 `MANUAL_SETUP.md` 和 `openclaw-scopes.json`。

### 6.3 发布与可用范围（两种方式都要做）

不论用 §6.1 还是 §6.2，发布后都要：

- **可用范围**：添加目标用户/部门（**协作者 ≠ 可使用用户**！协作者只能管理应用，不能用）
- 企业管理员审批通过后，目标用户才能在飞书客户端看到应用

详见 `doc/auto.md` 第 12 节关于发布和可用范围的踩坑经验。

---

## 7. 个性化修改指南

### 7.1 添加飞书快捷命令

编辑 `config/shortcuts.json`，让 `/cd <快捷名>` 自动展开成绝对路径：

```json
{
  "开发根目录": "D:\\ForRunning\\ForDev",
  "mycodex": "D:\\ForRunning\\ForDev\\openclaw",
  "指数复现": "D:\\ForRunning\\ForQuant\\projects\\recur_gz",
  "默认目录": "D:\\ForRunning\\ForDev\\0_default"
}
```

修改后**立即生效**，无需重启。客户在飞书发 `/cd mycodex` 即可切到对应绝对路径。

### 7.2 修改 mode 权限的 Bash 白/黑名单

可通过编辑 `config/approval_rules.json` 配置文件调整审批规则，无需修改 Python 源码，重启服务即可生效。

配置文件包含了三类判定规则：

- **高风险工具 (`high_risk_tools`)**：如 `Write`, `Edit`, `NotebookEdit`，匹配到直接送审。
- **安全命令白名单 (`safe_command_patterns`)**：在平衡模式 (mode m) 下自动放行的只读或安全命令前缀（如 `ls`, `git status`, `python --version` 等）。
- **高风险关键字黑名单 (`high_risk_keywords`)**：命令中包含这些高风险关键字（如 `rm `, `git push`, `npm install` 等）时优先拦截送审。

**常见修改场景**：

| 需求 | 编辑 `config/approval_rules.json` 改哪里 |
|---|---|
| 让 `docker ps` 自动放行 | 加到 `"safe_command_patterns"` 列表 |
| 让 `make` 强制审批 | 加到 `"high_risk_keywords"` 列表 |
| 让 `docker run` 强制审批 | 加到 `"high_risk_keywords"` 列表 |
| 让 `go test` 自动放行 | 加到 `"safe_command_patterns"` 列表 |

修改后**重启服务即可生效**（开发模式下修改配置文件亦可随时热更新）。

> 命令链 `cmd1 && cmd2` / `cmd1 ; cmd2` / `cmd1 \| cmd2` **只检查第一段**。所以 `safe_cmd && rm -rf /` 会被判低风险——这是已知妥协，生产环境建议 mode l 兜底。

### 7.3 添加 .bat 自启动（Windows）

#### 方式 1：使用现成的 bat

项目根目录已自带：

- `MyCodex.bat`：直接启动托盘 + 后端服务
- `MyCodex-Debug.bat`：开 console 模式，禁代理，检测端口占用，崩了不退出（看错误）
- `MyCodex-Restart.bat`：杀掉旧进程并重启服务（直接用 .venv 的 python 跑 restart_service.py）

把 `MyCodex.bat`（右键 → 创建快捷方式）放到启动文件夹：

```
Win+R → shell:startup → 回车 → 把快捷方式拖进去
```

开机后会自动启动。

#### 方式 2：自定义 bat

```bat
@echo off
chcp 65001 >nul 2>&1
title MyCodex Service
cd /d D:\ForRunning\ForDev\mycodex

:: 清理代理（避免飞书 API 走错出口）
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set http_proxy=
set https_proxy=
set all_proxy=

:: 检查端口是否被占
curl.exe --silent --fail --max-time 1 http://127.0.0.1:8090/health >nul 2>&1
if not errorlevel 1 (
    echo MyCodex is already running.
    timeout /t 3 >nul
    exit /b 0
)

:: 启动
.venv\Scripts\python.exe -m app.main
```

#### 方式 3：系统托盘版（推荐生产部署）

```bash
# 启动托盘版（最小化到系统托盘，双击图标看状态，右键退出）
.venv\Scripts\pythonw.exe scripts\tray.pyw
```

托盘版特点：

- 用 Windows Job Object 绑定子进程，托盘退出时 MyCodex 服务也退出（不会留孤儿进程）
- 启动时自动检查 `http://127.0.0.1:8090/health`，已运行则不重复启动
- 通过 `Global\MyCodexTray` 互斥锁防止多开
- 异常退出写 `mycodex-tray-error.log` + 弹 MessageBox

把 `pythonw.exe scripts\tray.pyw` 的快捷方式放到 `shell:startup` 即开机自启。

### 7.4 修改默认超时

`.env` 里：

```
APPROVAL_TIMEOUT=600            # 模型选择卡片超时（10min）
TOOL_APPROVAL_TIMEOUT=1800      # 工具审批总超时（30min）
TOOL_APPROVAL_WARN_SECONDS=300  # 超时前催办窗口（5min）
```

### 7.5 修改上下文阈值

`.env` 里：

```
CONTEXT_WARN_PERCENT=80         # 黄警（建议 /compact）
CONTEXT_CRITICAL_PERCENT=95     # 红警（建议 /new）
```

> 注意：实际生效的是 `config/settings.py` 里的 `context_warn_percent` / `context_critical_percent`。`.env` 修改后重启生效。

### 7.6 调整默认审批模式

`.env` 里：

```
APPROVAL_MODE=m                 # h=严格 m=平衡 l=全自动
```

只影响**首次启动**；用户在飞书里 `/mode X` 后写到 `.preferences/{open_id}.json`，覆盖默认。

### 7.7 /compact 功能

内置功能，无开关。用户发 `/compact` 压缩当前会话上下文；上下文超阈值时告警文案会建议 `/compact` 或 `/new`。

---

## 8. 部署验证清单

部署完后逐项验证（全部 ✅ 才算成功）：

| # | 验证项 | 命令/操作 | 期望 |
|---|---|---|---|
| 1 | 健康检查 | `curl http://localhost:8090/health` | `{"status":"ok","ws_connected":true,...}` |
| 2 | 飞书收消息 | 在飞书发 "hello" | 看到流式卡片回复 |
| 3 | /status | 飞书发 `/status` | 看到 session_id、workspace、model/effort/mode |
| 4 | /cd 列表 | 飞书发 `/cd`（不带参数） | 弹卡片列出本机历史工作区 |
| 5 | 历史继承 | 切到老工作区发消息 | `/status` 显示消息数 > 0（说明 `resume` 生效） |
| 6 | mode h 审批 | `/mode h` 后让 codex 写文件 | 看到审批卡片 |
| 7 | mode l 直通 | `/mode l` 后让 codex 写文件 | 无审批直接执行 |
| 8 | 运行日志 | `tail -f logs/mycodex.log` | 无 ERROR / Exception |
| 9 | 审计日志 | `tail -f logs/audit.log` | 能看到 `command_received` 记录 |
| 10 | 卡片回调 | 点审批卡片按钮 | 看到 toast "已允许/已拒绝" |
| 11 | 模型切换 | `/model <另一个 slug>` | 看到切换成功提示 |
| 12 | 异常自愈 | 强杀 codex 子进程 | 收到飞书异常退出告警 |

---

## 9. 常见故障排除

### 9.1 启动报 `FileNotFoundError: codex`

`shutil.which("codex")` 找不到。检查：

```bash
where codex              # Windows
which codex              # Linux/Mac
```

如果没有，安装 Codex CLI：`npm i -g @openai/codex`。

如果路径特殊，在 `.env` 里设 `CODEX_CLI_PATH` 为绝对路径。

### 9.2 Codex 未登录

模型调用依赖本机 ChatGPT 登录态。执行：

```bash
codex login status    # 查看登录态
codex login           # 浏览器授权登录
```

### 9.3 飞书消息收不到

检查顺序：

1. `curl http://localhost:8090/health` 的 `ws_connected` 是否为 `true`
2. 飞书开放平台 → 事件订阅 → 是否选了**长连接**模式
3. 添加的事件是否是 `im.message.receive_v1`
4. 应用是否已发布且通过审批
5. 当前用户是否在可用范围内
6. `mycodex.log` 里有没有 `From ou_xxx:` 日志（有就说明收到了）

如果Auto feishu 跑过但事件订阅没配好，看 `auto_feishu/feishu-app-result.json` 的 `eventSubscriptionConfigured` 是否 `true`；为 `false` 说明那一步失败了，重跑 `setup.cmd` 或按 §6.2 手工补。

### 9.4 卡片按钮点了无反应

检查：

1. 飞书开放平台 → 回调配置 → 是否选了**长连接**模式
2. 添加的回调是否是 `card.action.trigger`
3. `mycodex.log` 里有没有 `Card action: type=... act=...` 日志
4. 卡片模板里 `value.type` / `value.act` 是否和 router 里匹配

### 9.5 端口 8090 被占用

```bash
netstat -ano | findstr :8090    # Windows
```

要么 kill 占用进程，要么改 `.env` 的 `PORT`（同时改 `auto_feishu/config.json` 的 `localServiceUrl` 保持一致）。

### 9.6 进程异常退出循环

看 `mycodex.log` 末尾的 codex stderr 行，常见原因：

- ChatGPT 登录态失效（重新 `codex login`）
- workspace 路径不存在（`/cd` 切到有效目录）
- Codex CLI 版本太旧（`npm update -g @openai/codex`）

### 9.7 Windows 中文乱码 / 脚本编码

安装与启动脚本（`MyCodex.bat` / `MyCodex-Setup.bat` / `MyCodex-Restart.bat` / `auto_feishu\setup.cmd`）统一为 **GBK(ANSI) 编码 + CRLF 换行，不带 `chcp`**。

> ⚠️ 不要把这些脚本重新保存为 UTF-8：UTF-8 + `chcp 65001` 的 .bat 在 Win10/11 上有已知解析 bug（含中文的行会破坏下一行的执行，下一行被当命令报错）。编辑脚本务必用可按 ANSI/GBK 保存的编辑器。

### 9.8 Auto feishu 反复失败

参见 §6.1.9 排查表。如果反复卡在同一步（典型是权限导入或事件订阅），通常原因是飞书页面改版——看 `auto_feishu/artifacts/screenshots/` 最新截图定位变化点。短期兜底是改用 §6.2 手工方案，长期是更新 `auto_feishu/src/feishu-setup.ts` 里的选择器。

---

## 10. 项目结构索引

```
mycodex/                              # 项目根目录
├── .env                             # 环境变量（Auto feishu 会自动写飞书字段）
├── CLAUDEME.md                      # 本文件（Agent 配置指南）
├── config/
│   └── settings.py                  # pydantic-settings 配置定义
├── app/
│   ├── main.py                      # FastAPI 入口 + WS 长连接 + /health 端点
│   ├── profiles.py                  # Codex 模型注册表（models_cache 解析 + test_codex_auth）
│   ├── feishu/
│   │   ├── client.py                # 飞书 API 客户端
│   │   ├── events.py                # 消息分发 + 命令路由
│   │   ├── cards.py                 # 飞书卡片模板
│   │   └── ws.py                    # WebSocket 长连接
│   ├── agent/cli_loop.py            # Codex CLI 子进程管理 + JSONL 解析
│   ├── agent/codex_sessions.py      # codex 会话历史（thread_id 续聊）
│   ├── approval/manager.py          # 审批状态机（Future + 超时）
│   ├── audit/logger.py              # JSON-lines 审计日志
│   ├── models/schemas.py            # 数据模型
│   └── state/preferences.py         # per-user 的 model/effort/mode 持久化
├── examples/                        # 环境变量模板目录
│   └── .env.example                 #   .env 样例模板
├── logs/                            # 运行时日志目录
│   ├── mycodex.log                   #   运行日志（10MB × 5 轮转）
│   └── audit.log                    #   审计日志（JSON-lines）
├── auto_feishu/                     # ★ 飞书一键自动化工具（Node.js + Playwright）
│   ├── README.md                    #   自动化说明
│   ├── setup.cmd                    #   Windows 一键入口
│   ├── package.json                 #   Node 依赖（playwright / tsx / typescript）
│   ├── config.json                  #   自动化行为配置（appName / 事件 / 发布等）
│   ├── feishu-permissions.json      #   社区标准 22+3 scope 权限定义
│   ├── feishu-app-result.json       #   交付结果（不含 Secret，含 appId/maskedSecret）
│   ├── src/
│   │   ├── feishu-setup.ts          #   主自动化脚本
│   │   └── feishu-observe.ts        #   仅观察录制（不改配置）
│   ├── scripts/
│   │   └── mycodex-post-setup.mjs    #   后置钩子
│   └── artifacts/                   #   截图 / HTML / storage state（别提交 Git）
├── scripts/
│   ├── tray.pyw                     # Windows 系统托盘版启动器
│   ├── restart_service.py           # 重启服务工具
│   ├── stop_mycodex.ps1             # 停止/清理残留进程
│   ├── setup_autostart.bat          # Windows 开机自启快捷方式生成脚本
│   ├── start.sh                     # Linux/Mac 启动脚本
│   └── feishu_bot/                  # ⚠️ 早期自动化废弃产物（仅 MANUAL_SETUP.md 和 openclaw-scopes.json 有用）
├── doc/                             # 文档
├── flow/                            # 架构图
├── .preferences/                    # per-user 运行时偏好（自动生成）
├── .sessions/                       # per-user session 路由状态（自动生成）
├── MyCodex.bat / MyCodex-Restart.bat  # Windows 启动器
├── pyproject.toml                   # Python 项目配置
└── uv.lock                          # 依赖锁
```

---

## 11. 进一步文档

- `doc/report.md` — 客户向产品介绍和魔法指令用法
- `auto_feishu/README.md` — Auto feishu 工具的简版说明
- `scripts/feishu_bot/MANUAL_SETUP.md` — 飞书手工配置 8 步流程（兜底方案）
- `doc/TROUBLESHOOTING.md` — 飞书 SDK 详细踩坑（loop 问题、卡片回调、API 注意事项）
- `doc/auto.md` — 飞书开放平台自动化经验（Monaco 编辑器、checkpoint、发布流程等）
- `flow/architecture.html` — HTML 架构图
- `flow/ASYNC_FLOW_MERMAID.md` — Mermaid 流程图
