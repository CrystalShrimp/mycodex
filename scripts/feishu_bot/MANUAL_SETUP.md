# 给客户配飞书机器人 — 操作手册

> 来源：[AlexAnys/openclaw-feishu](https://github.com/AlexAnys/openclaw-feishu) 社区维护的 OpenClaw 飞书配置标准。
> 每个客户预计 **15-20 分钟**，绝大多数是手工点击。

## 准备

- 客户的飞书账号（要能登录 [open.feishu.cn](https://open.feishu.cn)）
- 客户的 OpenClaw 已装好（`openclaw gateway status` 能跑）
- scope JSON 文件：`scripts/feishu_bot/openclaw-scopes.json`（22 个 tenant + 3 个 user scope，社区标准）

---

## 步骤（按顺序）

### 1. 创建飞书应用（1 分钟）

客户在 https://open.feishu.cn 用飞书账号登录 → 点 **「创建企业自建应用」** → 填：
- 应用名称（如「XX 公司 AI 助手」）
- 描述
- 图标（可选，之后改也行）

### 2. 启用机器人能力（30 秒）

进应用 → 左侧 **应用能力 → 机器人** → **开启** → 给机器人起名

### 3. 导入权限（1 分钟，**核心节省时间的一步**）

左侧 **权限管理** → 点 **「批量导入」** 按钮 → 弹出 Monaco 编辑器 → **粘贴 `openclaw-scopes.json` 全部内容** → 点 **「下一步，确认新增权限」** → 点 **「申请开通」**

> 这一步把 25 个权限一次性开好。手工一个个勾至少 15 分钟。

### 4. 抓凭证（30 秒）

左侧 **凭证与基础信息** → 复制：
- **App ID**（格式 `cli_xxxxxxxxx`）
- **App Secret**（点「显示」按钮才出现）

⚠️ Secret 务必妥善保管。

### 5. 在 OpenClaw 里配飞书渠道（1 分钟）

在客户机器终端：

```bash
openclaw channels add
# 选择 Feishu → 粘贴 App ID → 粘贴 App Secret
openclaw gateway restart
```

> OpenClaw ≥ 2026.2 内置飞书插件，不用额外 `plugins install`。

### 6. 回飞书后台配事件订阅（1 分钟，**必须在第 5 步之后做**）

> ⚠️ **顺序很重要**：网关必须先启动，否则「使用长连接」选项保存会失败。

左侧 **事件与回调 → 事件配置**：
- 请求方式选 **「使用长连接接收事件」**（不是 Webhook）
- 添加事件：搜 `im.message.receive_v1`，勾选
- 保存

### 7. 发布应用（30 秒）

左侧 **版本管理与发布** → **创建版本** → 填版本说明 → **提交**

企业内部应用通常自动通过。

### 8. 测试 + 配对授权（1 分钟）

客户在飞书里搜机器人名字 → 发条消息「你好」→ 机器人会回复**配对码**（一串字母数字）。在客户机器终端跑：

```bash
openclaw pairing approve feishu <配对码>
```

之后再发消息，正常回复 = 完成 ✅

---

## 常见坑

| 现象 | 原因 | 解决 |
|---|---|---|
| 机器人没有消息发送框 | 事件订阅没配 | 第 6 步补上 + 重新发布版本 |
| 时断时续 | 网络/代理把 `open.feishu.cn` 走了代理 | 代理规则里把 `feishu.cn` 设为直连 |
| 配对码一直没消 | 第一次需要批准 | 第 8 步的 `pairing approve` |
| 群里机器人不回 | 默认要 @ 机器人 | 群里 @机器人，或改 `requireMention: false` |
| 发图片 AI 看不到 | 缺 `im:resource` 权限 | 检查 scope JSON 是否完整粘贴 |
| API 月度配额超限 | 多台机器共用一个 App，每 60s 探测一次 `bot/v3/info` | 只在一台机器启用飞书渠道 |

---

## 已废弃的自动化尝试

`scripts/feishu_bot/` 下还有这些文件，是之前尝试全自动化失败的产物，**已废弃不用**：

- `recorder.py` — 录制器（跑通过）
- `replayer.py` — 回放器（卡在 csrf）
- `diagnose_session.py` — csrf 诊断脚本（没跑通）
- `HANDOFF.md` — 失败交接文档
- `recording/` — 录制数据
- `artifacts/` — 失败时的状态快照

可以保留作历史参考，或直接 `rm -rf scripts/feishu_bot/{recorder,replayer,diagnose_session,help,HANDOFF}.py.md recording/ artifacts/` 清掉。`openclaw-scopes.json` 是本手册用到的，**别删**。

---

## 来源

- [AlexAnys/openclaw-feishu](https://github.com/AlexAnys/openclaw-feishu) — 社区维护的 OpenClaw 飞书配置指南
- [miaoxworld/OpenClawInstaller](https://github.com/miaoxworld/OpenClawInstaller/blob/main/docs/feishu-setup.md) — OpenClaw 安装器
- [larksuite/cli](https://github.com/larksuite/cli) — 飞书官方 CLI（管用 app，不管创建 app）
