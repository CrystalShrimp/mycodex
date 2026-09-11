# -*- coding: utf-8 -*-
"""群聊支持补丁：
1. on_message_receive: 提取 chat_type、剥离 @机器人 前缀
2. ReplyChannel: 群消息回群、私聊回私
3. _dispatch 内直接回复改走 ReplyChannel（配置流程卡片保持私聊）
4. _run_claude / send_and_wait 线程化 chat_id（进度/结果卡片跟随来源会话）
"""
import re

path = r"D:/ForRunning/ForDev/myclaw/app/feishu/events.py"
src = open(path, encoding="utf-8").read()

# ---------- 1. on_message_receive: chat_type + mention 剥离 ----------
old_entry = """    text = _parse_message_text(msg.content or "{}")
    if not text:
        return
    logger.info("From %s: %s", open_id, text[:100])
    task = asyncio.get_running_loop().create_task(
        _dispatch(open_id, chat_id, message_id, text)
    )"""
new_entry = """    text = _parse_message_text(msg.content or "{}")
    if not text:
        return
    # 群聊里 @机器人 的消息带 @_user_N 提及标记，剥掉再当指令/提示词
    text = re.sub(r"@_user_\\d+", "", text).strip()
    if not text:
        return
    chat_type = getattr(msg, "chat_type", "") or ""
    is_group = chat_type == "group"
    logger.info("From %s (%s): %s", open_id, chat_type or "p2p", text[:100])
    task = asyncio.get_running_loop().create_task(
        _dispatch(open_id, chat_id, message_id, text, is_group=is_group)
    )"""
assert old_entry in src, "entry block not found"
src = src.replace(old_entry, new_entry)

# ---------- 2. ReplyChannel 类（放在 SessionManager 之前） ----------
anchor = "# ===== Session management ====="
assert anchor in src
channel_cls = '''class ReplyChannel:
    """把回复路由到消息来源：群消息回群，私聊回私。
    配置流程类卡片（供应商/档位/模式选择、重用确认）不走这里，保持私聊。"""

    def __init__(self, open_id: str, chat_id: str, is_group: bool) -> None:
        self.open_id = open_id
        self.chat_id = chat_id
        self.is_group = is_group

    async def text(self, content: str) -> dict:
        if self.is_group and self.chat_id:
            return await feishu_client.send_text(self.chat_id, content, is_chat=True)
        return await feishu_client.send_text(self.open_id, content)

    async def card(self, card: dict) -> dict:
        if self.is_group and self.chat_id:
            return await feishu_client.send_card(self.chat_id, card, is_chat=True)
        return await feishu_client.send_card(self.open_id, card)


''' + anchor
src = src.replace(anchor, channel_cls, 1)

# ---------- 3. _dispatch: 签名 + reply 通道 + 函数体内替换 ----------
m = re.search(r"^async def _dispatch\(([^)]*)\)", src, re.M)
assert m, "_dispatch signature not found"
src = src.replace(m.group(0),
    "async def _dispatch(open_id: str, chat_id: str, message_id: str, text: str, is_group: bool = False) -> None:")

# 定位 _dispatch 函数体范围（到下一个顶层 async def / def 为止）
start = src.index("async def _dispatch(")
nxt = re.search(r"^\n(?:async )?def \w+", src[start + 10:], re.M)
body_end = start + 10 + (nxt.start() if nxt else len(src))
body = src[start:body_end]

# 私聊保留清单：数据目录引导卡（首启配置流程）
keep_private_patterns = ["_claude_dir_selection_card()"]

# 替换直接回复
count_t = body.count("await feishu_client.send_text(open_id,")
count_c = body.count("await feishu_client.send_card(open_id,")
new_body = body.replace("await feishu_client.send_text(open_id,", "await reply.text(")
new_body = new_body.replace("await feishu_client.send_card(open_id, _claude_dir_selection_card())",
                            "await feishu_client.send_card(open_id, _claude_dir_selection_card())  # 配置流程：保持私聊")
new_body = new_body.replace("await feishu_client.send_card(open_id,", "await reply.card(")
# 恢复被误替换的私聊保留行
new_body = new_body.replace("await reply.card( _claude_dir_selection_card())  # 配置流程：保持私聊",
                            "await feishu_client.send_card(open_id, _claude_dir_selection_card())  # 配置流程：保持私聊")

# 在函数体开头插入 reply 构造
docstring_anchor = "    if not _is_user_allowed(open_id):"
assert docstring_anchor in new_body, "dispatch first line not found"
new_body = new_body.replace(docstring_anchor,
    "    reply = ReplyChannel(open_id, chat_id, is_group)\n" + docstring_anchor, 1)

src = src[:start] + new_body + src[body_end:]

# ---------- 4. _run_claude 线程化 chat_id ----------
old_sig = "async def _run_claude(\n    prompt: str,\n    open_id: str,\n    session: Session | None = None,\n    skip_classify: bool = False,\n    resume_session_id: str | None = None,\n) -> None:"
if old_sig not in src:
    m2 = re.search(r"async def _run_claude\([^)]*\)", src)
    src = src.replace(m2.group(0),
        "async def _run_claude(\n    prompt: str,\n    open_id: str,\n    session: Session | None = None,\n    skip_classify: bool = False,\n    resume_session_id: str | None = None,\n    chat_id: str = \"\",\n) -> None:")
else:
    src = src.replace(old_sig, old_sig[:-1] + '    chat_id: str = "",\n) -> None:')

# send_and_wait 调用透传 chat_id
old_call = "agent_result = await claude_cli_loop.send_and_wait("
i = src.index(old_call)
j = src.index(")", src.index("resume_session_id", i)) if "resume_session_id" in src[i:i+600] else -1
# 直接在该调用的参数块末尾（第一个闭合括号处）加 chat_id
depth = 0
for k in range(i + len(old_call) - 1, len(src)):
    if src[k] == "(":
        depth += 1
    elif src[k] == ")":
        if depth == 0:
            src = src[:k] + ", chat_id=chat_id" + src[k:]
            break
        depth -= 1

open(path, "w", encoding="utf-8").write(src)
import ast
ast.parse(src)
print(f"events.py patched: dispatch 内替换 text={count_t} card={count_c} 处；语法 OK")
