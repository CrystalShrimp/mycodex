# mycodex 飞书一键配置

客户在 Windows 终端进入本目录后只需运行：

```bat
setup.cmd
```

脚本会自动安装锁定依赖、准备 Chromium 并打开飞书开放平台。客户只需在浏览器登录一次飞书账号，登录成功后脚本自动继续，无需返回终端操作。

## 自动完成

- 创建或复用飞书企业自建应用
- 读取 App ID / App Secret，只写入 mycodex 根目录 `.env`
- 启用机器人并导入 `feishu-permissions.json`
- 启动或校验本地 mycodex 及飞书 WebSocket
- 订阅 `im.message.receive_v1` 和 `card.action.trigger`
- 创建并发布应用版本
- 写入不含 Secret 的 `feishu-app-result.json`

脚本可重复运行，会复用应用、登录状态和 `.env` 凭据，并跳过已经完成的步骤。

## 完成与测试

看到以下提示即表示交付完成：

```text
交付完成：.env 已写入飞书凭据，结果 JSON 未保存 Secret，可启动本地 claw 并在飞书测试机器人。
```

随后在飞书向机器人发送消息，确认本地 mycodex 收到并返回结果。

## 失败恢复

除首次登录外，脚本不会要求客户手工接管。遇到验证码、租户审批、组织策略、权限不足或页面变化时，会直接输出失败步骤、错误原因、截图和 HTML 快照路径。解除外部阻挡后重新运行 `setup.cmd` 即可续跑。

调试人员可运行：

```bat
npm run feishu:setup:debug
```
