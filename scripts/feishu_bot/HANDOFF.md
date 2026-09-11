# Feishu Bot 自动化 — 交接文档

> **警告**：本文档面向接管此任务的下一个模型/工程师。当前实现只验证了「录制」这一步，「回放」8 步全部未跑通——只跑到第 1 步的 csrf 校验就失败了。下面所有「未验证」的部分都要重新审视。

---

## 1. 目标

自动化飞书（Lark）开发者后台 https://open.feishu.cn 的应用配置全流程：

1. 创建企业自建应用
2. 抓 App ID + App Secret
3. 导入权限 scope（按 `feishu-permissions.json` 列表）
4. 启用机器人能力
5. 配置事件订阅（WebSocket 长连接 + 添加事件）
6. 创建版本
7. 提交发布

**用直调内部 API 代替 UI 自动化**（prev_work 的 TS 方案因为 Monaco 编辑器粘贴不稳定而失败 17 分钟，详 `prev_work/feishu-app-result.json`）。

## 2. 技术栈

- Python 3.12+，项目用 uv 管理依赖（`pyproject.toml`）
- playwright：**只用来 CDP attach** 到用户已登录的 Chrome，**不下浏览器二进制**（用户网络下不动）
- httpx（项目自带）：当前未用，备选
- Windows 11，用户用 `chrome.exe --remote-debugging-port=9222 --user-data-dir=...` 启动 Chrome 并扫码登录

## 3. 已验证稳定（这部分不用改）

| 组件 | 状态 |
|---|---|
| `scripts/feishu_bot/recorder.py` | ✅ 跑通一次，录到 1.9MB / 426 个事件 |
| API 端点全量识别 | ✅ 从录制中提取 |
| `prev_work/feishu-permissions.json` | ✅ scope 列表已验证（22 tenant + 3 user） |

## 4. 未验证（**整个回放器 8 步全部没跑通**）

> **重要**：之前的描述说「当前唯一阻塞是 csrf」是误导。事实是：**只跑了第 1 步、卡在 csrf，后面 7 步一行都没执行过**。下面每一步都有独立的失败风险，请逐条审视。

### 步骤 1：创建应用 — BLOCKED（csrf）

- 端点：`POST https://open.feishu.cn/developers/v1/app/create`
- 请求体（录制抓到）：
  ```json
  {
    "appSceneType": 0,
    "name": "OpenClaw 助手",
    "desc": "用于接入 OpenClaw 的飞书机器人",
    "i18n": {"zh_cn": {"name": "...", "description": "..."}},
    "primaryLang": "zh_cn"
  }
  ```
- **失败点**：返回 `{"code":9499,"msg":"csrf token check fail"}`
- **其他风险**：
  - `appSceneType: 0` 含义未知，飞书是否限制类型？
  - 同名应用冲突时如何处理？（录制时这个 app 是全新创建的）
  - 头像 `avatar` 字段：录制时有，但来自前置 `/upload/image`。replayer 里去掉了，**可能导致缺省头像**——不知道是否阻塞创建。

### 步骤 2：抓 App Secret — UNTESTED

- 端点：`POST /developers/v1/secret/{appId}`，body `{}`
- 录制响应：`{"code":0,"data":{"secret":"xkeThY9wRkhrtHLIDK3GWdrrAIa2XaKB"}}`
- **风险**：
  - 录制时是新 app，secret 直接返回。但飞书后台通常需要点「显示」按钮才显示 secret——这个 API 是否真的不需要前置操作？需验证。
  - 如果飞书策略对该 API 加了组织级开关，调用会失败。

### 步骤 3：建 scope name→id 映射 — UNTESTED，**高风险**

- 端点：`POST /developers/v1/scope/all/{appId}`，body `{}`
- 录制响应 30014 字符（**被 recorder 截断了**，真实更长）
- **已知字段**：`data.scopeBizs[]`（业务分类列表，每项含 `bizId, bizName`）
- **未知字段**：scope 列表本身在哪个 key 下？items 嵌套在哪？
- **当前代码（`step_build_scope_map`）的猜测逻辑**：
  - 先试 `data.scopes` / `data.allScopes` / `data.scopeList`
  - 兜底遍历 `data.scopeBizs[].items[]`
  - 每个 item 取 `name`/`scopeName`/`key` 和 `id`/`scopeId`/`scopeID`
- **如果实际字段名不一样，整个映射就是空的**，第 4 步必然失败。
- **建议验证**：跑一次脚本，打印 `/scope/all` 完整响应结构，对照修改。

### 步骤 4：导入权限 — UNTESTED，**高风险**

- 端点：`POST /developers/v1/scope/update/{appId}`
- body 关键字段：
  ```json
  {
    "appScopeIDs": ["1014140", "1014139", ...],  // 数字 ID
    "userScopeIDs": ["1014140", "1014139", "1014164"],
    "scopeIds": [],
    "operation": "add",
    "isDeveloperPanel": true
  }
  ```
- **风险**：
  - ID 是 **全局数字 ID**（不是 app 内 ID），来自 step 3 的映射
  - 录制时 22 个 tenant scope → 23 个 ID（**对不上**，可能录制时有重复或我数错）
  - scope 名字命名约定可能变（飞书改 API）
  - **scope 名字 vs scope key 的混淆**：`feishu-permissions.json` 里的 `im:message` 是 scope key，但 `/scope/all` 返回的 `name` 字段可能是中文名（如「获取与发送单聊、群组消息」），不是 scope key。**这是最可能踩的坑**——需要找返回的另一个字段（可能是 `key` / `scope` / `apiName`）来做匹配。
  - 导入后是否需要调别的接口「申请开通」？录制里看到过 `/privilege/update`，replayer **没调用它**——可能是缺失步骤。

### 步骤 5：启用机器人 — UNTESTED

- 端点：`POST /developers/v1/robot/switch/{appId}` body `{"enable": true}`
- **风险**：
  - 录制时还调用了 `/developers/v1/robot/{appId}`（其他 body），replayer 没调
  - 机器人名/头像/描述是否必填？prev_work 的 TS 代码有「设置机器人名称」步骤，replayer **没做**

### 步骤 6：配置事件订阅 — UNTESTED，**高风险**

- 三个连续调用：
  1. `POST /event/switch/{appId}` body `{"eventMode": 4}` （4 = WebSocket 长连接）
  2. `POST /callback/switch/{appId}` body `{"callbackMode": 4}`
  3. `POST /event/update/{appId}` body `{"operation":"add","appEvents":["im.message.receive_v1","im.message.message_read_v1"],"userEvents":[],"eventMode":4}`
- **风险**：
  - 录制时事件订阅的「保存」**依赖 openclaw 网关在跑**（prev_work README 明确说了：「OpenClaw 已添加 Feishu 渠道且网关正在运行，否则长连接设置可能保存失败」）
  - 用户当前没启动 openclaw 网关——这步可能直接 422/500
  - 事件名是否需要其他前置（开通 im:message scope 等）

### 步骤 7：创建版本 — UNTESTED，**高风险**

- 端点：`POST /developers/v1/app_version/create/{appId}`
- body 极复杂，含 `visibleSuggest`、`applyReasonConfig`、`b2cShareSplitConfigSuggest`、`autoPublish`、`blackVisibleSuggest`
- **关键风险**：
  - 录制时 `visibleSuggest.members = ["7171432364134989825"]`，这是**用户自己的 member ID**，每个账号不一样
  - replayer 里设 `isAll: 1`（全员可见）当 `owner_member_id=None`——这是**猜测**，不知道飞书是否接受
  - `applyReasonConfig` 一堆 bool—— 全设成 false，可能不符合组织策略
  - 版本号 `1.0.0`—— 是否有格式约束？
  - 如果上一步没正确开通权限/能力，这一步会失败

### 步骤 8：发布 — UNTESTED

- 端点：`POST /developers/v1/publish/commit/{appId}/{versionId}` body `{}`
- **风险**：
  - 触发组织审批流程，可能不返回 `code:0` 而是「待审批」状态
  - replayer 把非 `code:0` 当失败抛异常——但「待审批」其实不算失败
  - 用户如果不是组织管理员，发布可能直接拒绝

---

## 5. CSRF 机制专项调查（步骤 1 的根因）

### 已知事实
- 录制时所有 `/developers/v1/*` POST 都带 header `x-csrf-token: av9Iq7SrXj5IvL5dmr69aa5IKrLmIJv2K9j+bO7WZY+yCPjkO7LVrONdWEUQr++UbdmZoZ454nINTFYM4Z4gvg==`（88 字符 base64）
- **该值只出现在 request header 里**——录制 events.jsonl 中**不在任何 response body、Set-Cookie、URL**
- 录制中有 `POST https://internal-api-lark-api.feishu.cn/accounts/csrf?_t=...` 请求，响应 `{"code":0,"message":"ok"}`，**响应里也没 Set-Cookie**

### 当前浏览器 `document.cookie` 暴露的 csrf 相关 cookie
- `lark_oapi_csrf_token`
- `swp_csrf_token`

### 已试方案（全部失败）
| 方案 | 结果 |
|---|---|
| httpx + ctx.cookies() 找 csrf-token / csrfToken / x-csrf-token / csrf_token | 找不到这些名字 |
| httpx + 用 `lark_oapi_csrf_token` cookie 值作为 x-csrf-token | `csrf token check fail` |
| page.evaluate + fetch，不主动加 csrf，期望前端拦截器自动加 | `x-csrf-token not exist in header`（前端没 monkey-patch fetch） |
| page.evaluate + fetch，从 `document.cookie` 读 `lark_oapi_csrf_token` 作为 x-csrf-token | `csrf token check fail`（发了但不对） |

### 还没试的方向（推荐排查顺序）
1. **拦截真实请求读 token**（最可靠）：用 `page.route("**/*", handler)` 拦截页面自己发的 `/developers/*` POST，从 `await request.all_headers()` 读 `x-csrf-token`。`replayer.py` 里已经加了这个函数 `capture_csrf_token(page)`，但**未验证能否抓到**——`all_headers()` 是否含自定义 header 需测试
2. **`swp_csrf_token` 才是对的**（只试了 `lark_oapi_csrf_token`）
3. **token 是某 cookie 的派生值**（HMAC/base64 编码某个 session cookie），不是直接读取
4. **token 来自 HTML `<meta>` 标签**——grep `tmp_feishu_import_snapshot.html` 找 88 字符 base64
5. **token 在前端 JS 全局对象里**——`page.evaluate("Object.keys(window)")` 翻一遍
6. **token 由 `/accounts/csrf` 接口的某种隐藏机制下发**（Set-Cookie 被 playwright 漏抓？response body 的非默认字段？）

---

## 6. 文件清单

```
D:\ForRunning\ForDev\openclaw\
├── scripts\feishu_bot\
│   ├── recorder.py                      # 录制器（已跑通）
│   ├── replayer.py                      # 回放器（卡在 step 1，全文未验证）
│   ├── HANDOFF.md                       # 本文件
│   ├── recording\
│   │   ├── events.jsonl                 # 1.9MB，426 事件，录制数据
│   │   └── snapshots\*.html             # 11 个 DOM 快照
│   └── artifacts\
│       └── replay-result-*.json         # 失败时的状态快照
├── prev_work\
│   ├── feishu-permissions.json          # ✅ scope 列表，直接可用
│   ├── config.json                      # ✅ 参数模板
│   ├── tmp_feishu_import_snapshot.html  # 权限页 DOM（1130 行）
│   ├── src\
│   │   ├── feishu-setup.ts              # 之前的 TS 方案（参考）
│   │   └── feishu-observe.ts            # 之前的录制器（参考）
│   ├── feishu-app-result.json           # 之前失败的状态
│   └── artifacts\                       # 之前的截图、HTML dump
└── .claude\
    └── settings.json                    # ⚠️ openclaw 注入的 PreToolUse hook
```

## 7. 约束

1. **不下浏览器二进制**——用户网络下不动。只走 `connect_over_cdp`，绝不调 `playwright install`
2. **不要 `page.goto` 用户没授权过的页面**——用户对脚本控制浏览器极度敏感
3. **代码要可审计、可解释每一步在干什么**——用户怀疑过脚本失控
4. **不修改 `.claude/settings.json` 的 openclaw hook**，除非用户明确要求
5. **不主动开后台进程、不调度 cron**

## 8. 验收标准

`python scripts/feishu_bot/replayer.py` 跑完输出：
```
[1/8] 创建应用：OpenClaw 助手
      appId = cli_xxxxx
[2/8] 抓 App Secret
      secret = xxxx****xxxx
[3/8] 拉 scope 全量列表
      拿到 N 个 scope
[4/8] 导入权限
      导入 tenant x 22, user x 3
[5/8] 启用机器人能力
[6/8] 配置事件订阅（WebSocket）+ 添加 2 个事件
[7/8] 创建版本 1.0.0
      versionId = 7662xxxxxxxxxxxxx
[8/8] 提交发布
=== ✅ 全流程完成 ===
App ID:     cli_xxxxx
App Secret: xxxx****xxxx
```

且飞书开发者后台能看到新 app：权限 22+3 全开、机器人已启用、事件订阅配置好、版本已提交（不一定立即通过，但「待审批」状态可接受）。

## 9. 推荐工作流（给接管者）

1. 第一步先**验证 step 3 的 scope/all 响应结构**——这决定了 step 3+4 能否工作
2. 修 csrf（见第 5 节，推荐 page.route 拦截方案）
3. 一步一步跑通，**每步都加 dry-run 模式**（只打印请求不发）让用户预审
4. 跑通后整合，做参数化（app_name / scope 列表 / 事件列表 / 版本号）
