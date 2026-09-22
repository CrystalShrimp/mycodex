"""企微通道单元测试（无真实凭据，纯协议/策略层验证）。"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.channel.base import ProgressSnap, PLATFORM_WECOM, UserTarget
from config.settings import settings


# ---------- fakes ----------


class FakeConn:
    def __init__(self, incoming: list[dict] | None = None) -> None:
        self._incoming = [json.dumps(f) for f in (incoming or [])]
        self.sent: list[dict] = []
        self.closed = False

    async def recv(self):
        if self._incoming:
            return self._incoming.pop(0)
        await asyncio.sleep(3600)

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self):
        self.closed = True


class FakeWs:
    """替代 WeComWsClient：记录 request 调用，可注入响应。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str | None]] = []
        self.responses: list[dict] = []

    async def request(self, cmd, body, timeout=30.0, req_id=""):
        self.calls.append((cmd, body, req_id or None))
        if self.responses:
            return self.responses.pop(0)
        return {"errcode": 0, "errmsg": "ok"}


# ---------- ws 帧分发与排重 ----------


@pytest.mark.asyncio
async def test_ws_msgid_dedup():
    from app.wecom.ws import WeComWsClient

    got: list[dict] = []

    async def on_msg(body, req_id):
        got.append(body)

    client = WeComWsClient("bot", "sec", on_message=on_msg)
    frame = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r1"},
        "body": {"msgid": "m1", "msgtype": "text", "text": {"content": "hi"}},
    }
    client._conn = FakeConn([frame, dict(frame)])  # 同 msgid 两帧
    task = asyncio.get_running_loop().create_task(client._recv_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert len(got) == 1


@pytest.mark.asyncio
async def test_ws_request_correlation_with_external_req_id():
    from app.wecom.ws import WeComWsClient

    client = WeComWsClient("bot", "sec")
    conn = FakeConn([{"headers": {"req_id": "cb-req"}, "errcode": 0, "errmsg": "ok"}])
    client._conn = conn
    recv_task = asyncio.get_running_loop().create_task(client._recv_loop())
    resp = await client.request("aibot_respond_msg", {"msgtype": "text"}, req_id="cb-req")
    recv_task.cancel()
    try:
        await recv_task
    except asyncio.CancelledError:
        pass
    assert resp["errcode"] == 0
    assert conn.sent[0]["headers"]["req_id"] == "cb-req"  # respond 必须复用回调 req_id


# ---------- 流式进度：节流 / 限流 / 10min 换流 ----------


class FakeWecomClient:
    def __init__(self) -> None:
        self.stream_pushes: list[tuple[str, str, bool]] = []  # (stream_id, content, finish)

    async def respond_stream(self, req_id, stream_id, content, *, finish):
        self.stream_pushes.append((stream_id, content, finish))
        return {"errcode": 0, "errmsg": "ok"}


@pytest.mark.asyncio
async def test_progress_throttle_and_rate_limit(monkeypatch):
    from app.wecom import channel as ch

    clock = {"t": 1000.0}
    monkeypatch.setattr(ch.time, "monotonic", lambda: clock["t"])

    fake = FakeWecomClient()
    handle = ch.WeComProgressHandle(fake, "req", None, None)
    snap = ProgressSnap(model="sonnet", status="running")

    # 同状态 1s 内连发 5 次 → 只推 1 帧
    for _ in range(5):
        await handle.update(snap)
    assert len(fake.stream_pushes) == 1

    # 时间推进但同状态：2.5s 节流边界
    clock["t"] += 3.0
    await handle.update(snap)
    assert len(fake.stream_pushes) == 2

    # 30 条/分钟滑动窗口：状态快速翻转绕过同状态节流，窗口应封顶
    clock["t"] += 3.0
    base = len(fake.stream_pushes)
    for i in range(40):
        clock["t"] += 0.1
        status = "running" if i % 2 == 0 else "retrying"
        await handle.update(ProgressSnap(model="sonnet", status=status))
    # 60s 窗口内最多 30 帧
    assert len(fake.stream_pushes) - base <= 30
    # 且没有丢终态能力
    clock["t"] += 3.0
    await handle.finish(ProgressSnap(model="sonnet", status="completed", result_text="done"))
    assert fake.stream_pushes[-1][2] is True  # finish=True 结束流


@pytest.mark.asyncio
async def test_progress_roll_at_10min(monkeypatch):
    from app.wecom import channel as ch

    clock = {"t": 1000.0}
    monkeypatch.setattr(ch.time, "monotonic", lambda: clock["t"])

    fake = FakeWecomClient()
    handle = ch.WeComProgressHandle(fake, "req", None, None)
    first_stream = handle._stream_id

    await handle.update(ProgressSnap(model="m", status="running"))
    # 推进超过 9.5min 再更新 → 换流：旧流 finish、新流续传
    clock["t"] += 600.0
    await handle.update(ProgressSnap(model="m", status="running"))
    streams = {s for s, _, _ in fake.stream_pushes}
    assert first_stream in streams
    assert handle._stream_id != first_stream
    # 旧流被显式结束
    old_finish = [f for s, c, f in fake.stream_pushes if s == first_stream and "转下一条" in c]
    assert old_finish == [True]


# ---------- 编号降级选择与按钮 key 分发 ----------


@pytest.mark.asyncio
async def test_numbered_pick(monkeypatch):
    from app.wecom import events as we
    from app.dispatch import selections
    from app.wecom.channel import chat_key_of, pending_numbered

    target = UserTarget(platform=PLATFORM_WECOM, user_id="u1")
    pending_numbered[chat_key_of(target)] = {
        "kind": "file_selection",
        "payload": {},
        "options": [("/tmp/a.txt", "a.txt (1KB)")],
        "expires": time.time() + 300,
    }

    calls: list = []

    def fake_file_send(t, path):
        calls.append(path)
        return "已开始发送文件：a.txt"

    monkeypatch.setattr(selections, "handle_file_send", fake_file_send)

    acks: list[str] = []

    class FakeChannel:
        def note_req_id(self, *a):
            pass

        async def send_text(self, t, text):
            acks.append(text)

    monkeypatch.setattr(we, "_CHANNEL", FakeChannel())
    handled = await we._try_numbered_pick(target, "1")
    assert handled is True
    assert calls == ["/tmp/a.txt"]
    await asyncio.sleep(0.05)  # ack 在 await 链内同步完成
    assert acks and "a.txt" in acks[0]
    # 命中后 pending 被消费
    assert chat_key_of(target) not in pending_numbered


def test_parse_approval_key(monkeypatch):
    from app.wecom import events as we
    from app.dispatch import selections

    got: list = []

    def fake_decide(t, approval_id, approved):
        got.append((approval_id, approved))
        return "已允许"

    monkeypatch.setattr(selections, "decide_approval", fake_decide)
    target = UserTarget(platform=PLATFORM_WECOM, user_id="u1")
    ack = we._parse_and_dispatch(target, "mc:ap:approve:abc123def456")
    assert got == [("abc123def456", True)]
    assert ack == "已允许"


def test_parse_mode_key(monkeypatch):
    from app.wecom import events as we
    from app.dispatch import selections

    got: list = []

    def fake_mode(t, mode):
        got.append(mode)
        return "ok"

    monkeypatch.setattr(selections, "handle_mode_switch", fake_mode)
    target = UserTarget(platform=PLATFORM_WECOM, user_id="u1")
    we._parse_and_dispatch(target, "mc:md:l:")
    assert got == ["l"]


# ---------- 渲染与配置 ----------


def test_progress_render_final():
    from app.wecom.cards import render_progress_text

    snap = ProgressSnap(
        model="sonnet", status="completed", result_text="全部完成",
        input_tokens=1000, output_tokens=2000, task_id="t1",
    )
    text = render_progress_text(snap)
    assert "任务完成" in text and "全部完成" in text and "1,000" in text


def test_allowed_users_platform_filter(monkeypatch):
    monkeypatch.setattr(
        settings, "allowed_users",
        "ou_aaa, wecom:zhangsan",
        raising=False,
    )
    assert settings.get_allowed_users_for("feishu") == ["ou_aaa"]
    assert settings.get_allowed_users_for("wecom") == ["zhangsan"]


def test_enabled_channels_autodetect(monkeypatch):
    monkeypatch.setattr(settings, "channels", "", raising=False)
    monkeypatch.setattr(settings, "feishu_app_id", "cli_x", raising=False)
    monkeypatch.setattr(settings, "feishu_app_secret", "s", raising=False)
    monkeypatch.setattr(settings, "wecom_bot_id", "", raising=False)
    assert settings.get_enabled_channels() == ["feishu"]
    monkeypatch.setattr(settings, "wecom_bot_id", "b", raising=False)
    monkeypatch.setattr(settings, "wecom_secret", "w", raising=False)
    assert settings.get_enabled_channels() == ["feishu", "wecom"]
    monkeypatch.setattr(settings, "channels", "wecom", raising=False)
    assert settings.get_enabled_channels() == ["wecom"]
