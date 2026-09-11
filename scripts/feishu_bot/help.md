先不要继续尝试跑完整的 8 步流程。本轮目标仅限于：

1. 被动捕获浏览器当前会话中的真实 `x-csrf-token`
2. 用现有应用验证 `/scope/all/{appId}` 的完整响应结构
3. 不创建应用，不修改权限，不启用机器人，不发布版本

## 硬性约束

* 只使用 `connect_over_cdp` 连接用户已经登录的 Chrome。
* 禁止下载或安装 Playwright 浏览器。
* 禁止调用 `page.goto()`。
* 禁止自动点击会更改飞书配置的按钮。
* 禁止修改 `.claude/settings.json`。
* 禁止启动后台进程。
* 禁止在日志中打印完整 Cookie、App Secret 或完整 CSRF token。
* 本轮不要修改正式的 8 步执行逻辑，优先新增独立诊断脚本，例如：
  `scripts/feishu_bot/diagnose_session.py`

## 一、被动捕获真实 CSRF token

连接 CDP 后：

1. 枚举已有 browser contexts 和 pages。
2. 仅选择 URL origin 为 `https://open.feishu.cn` 的现有页面。
3. 如果不存在这样的页面，停止并明确报告，不要自行导航。
4. 在页面上注册被动网络监听，不得拦截、修改或重发原请求。

优先同时使用：

* `context.on("request")`
* `page.on("request")`
* CDP `Network.requestWillBeSent`
* CDP `Network.requestWillBeSentExtraInfo`

注意：

* `requestWillBeSentExtraInfo` 与 `requestWillBeSent` 的到达顺序不固定，需要按 `requestId` 缓存并合并。
* 只关注 URL 中包含 `/developers/v1/` 的 POST 请求。
* 从真实请求头中查找大小写不敏感的 `x-csrf-token`。
* 不要先猜 Cookie，不要继续尝试把 `lark_oapi_csrf_token` 或 `swp_csrf_token` 直接当请求头。
* 捕获时只打印：

  * 请求 URL
  * 请求方法
  * header 名称列表
  * token 长度
  * token 前 4 位和后 4 位
* token 完整值只能保存在当前进程内存中。

监听就绪后，提示用户在当前飞书页面手动执行一个只读取数据的操作，例如打开已有应用的“权限管理”页面。脚本本身不要导航或点击。

如果普通 request 事件拿不到自定义头，再使用 `page.route("**/*")` 作为后备方案，但 handler 只能读取 `request.all_headers()`，随后立即 `route.continue_()`，不得修改请求。

## 二、使用同一页面验证 token

取得 token 后，不要使用独立的裸 `httpx` 会话。

在相同的飞书页面中通过 `page.evaluate()` 发起同源请求，让浏览器自动携带当前 Cookie、Origin 和 Referer：

```javascript
fetch(`/developers/v1/scope/all/${appId}`, {
  method: "POST",
  credentials: "include",
  headers: {
    "content-type": "application/json",
    "x-csrf-token": token
  },
  body: "{}"
})
```

`appId` 的获取顺序：

1. 从当前页面 URL 提取已有应用的 `cli_...`
2. 从页面已有 API 请求 URL 提取
3. 从录制数据中选择仍然存在的应用 ID

禁止为了获得 appId 创建新应用。

保存以下结果：

* HTTP status
* 响应 JSON 中的 `code` 和 `msg`
* 完整响应 JSON，写入：
  `scripts/feishu_bot/artifacts/scope-all-full.json`

文件中不得包含 Cookie 或 CSRF token。

如果仍然返回 `9499`：

* 停止，不要继续枚举 Cookie 或猜 token 算法。
* 输出本次测试请求与真实请求之间的 header 名称差异。
* 输出真实请求的 `initiator` 信息。
* 检查真实请求是否还有其他关键自定义头，例如 `x-requested-with`，但不要打印敏感值。
* 不要进入创建应用步骤。

## 三、检查 `/scope/all` 响应结构

仅当响应 `code == 0` 时执行。

递归分析完整 JSON，输出：

* 所有数组字段的 JSON path
* 每个候选数组第一项的字段名
* 所有包含以下字段名或相似字段名的对象路径：

  * `id`
  * `scopeId`
  * `scopeID`
  * `key`
  * `name`
  * `scope`
  * `scopeName`
  * `apiName`
  * `permission`

不要假设 scope 一定在：

* `data.scopes`
* `data.allScopes`
* `data.scopeList`
* `data.scopeBizs[].items`

要根据实际响应确认。

然后读取：

`prev_work/feishu-permissions.json`

对其中 22 个 tenant scope 和 3 个 user scope 做精确匹配检查：

* 优先匹配 scope key，例如 `im:message`
* 不得用中文显示名称代替 scope key
* 每个 scope 必须恰好匹配一个数字 ID
* 输出未匹配项
* 输出一对多歧义项
* 输出重复 ID
* 输出 tenant 和 user 各自最终解析数量

只有满足以下条件，才可认为 scope 映射验证通过：

* tenant：22/22 唯一匹配
* user：3/3 唯一匹配
* 无未匹配项
* 无歧义项
* 映射值均为有效数字 ID

本轮禁止调用 `/scope/update/{appId}`。

## 四、给出本轮报告

程序结束时必须输出一份简洁报告：

```text
=== 诊断结果 ===
Feishu page: 已找到 / 未找到
真实 developers POST: 已捕获 / 未捕获
x-csrf-token: 已捕获 / 未捕获
token length: N
scope/all HTTP status: N
scope/all code: N
完整响应保存路径: ...
scope 数组实际 JSON path: ...
tenant scope: 已匹配 X/22
user scope: 已匹配 X/3
未匹配: [...]
歧义项: [...]
本轮是否发送配置变更请求: 否
```

同时列出本轮新增或修改的文件，并说明每个文件的用途。

不要声称已经修复完整回放器。当前验收目标仅为：

1. 捕获真实 CSRF token
2. 成功读取完整 `/scope/all` 响应
3. 准确确认 scope key 到数字 ID 的映射结构
