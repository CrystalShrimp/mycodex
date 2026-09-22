"""MyCodex 双端会话同步诊断与验证工具。

用于验证：
1. Codex CLI 本地存储 (~/.codex/sessions) 下当前工作区的 Rollout 线程状态；
2. 检查多轮消息及摘要提取；
3. 模拟飞书端自动感知电脑端最新活跃 Codex CLI 会话的判定结果。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 确保在 Windows 控制台下输出 UTF-8 不因 emoji 报错
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agent.codex_sessions import (
    codex_home,
    detect_cli_thread_update,
    iter_thread_messages,
    latest_thread_for_workspace,
    list_workspace_threads,
)


def inspect_workspace(workspace: str) -> None:
    ws_path = Path(workspace).resolve()
    print("=" * 60)
    print(f"🔍 正在诊断 Codex 工作区: {ws_path}")
    print("=" * 60)

    c_home = codex_home()
    print(f"📁 Codex 数据根目录: {c_home}")

    threads = list_workspace_threads(str(ws_path))
    print(f"📊 发现 {len(threads)} 个该工作区下的 Codex 会话线程 (按时间倒序)：\n")

    for idx, t in enumerate(threads[:10], 1):
        tid = t["thread_id"]
        count = t["message_count"]
        summary = t["last_summary"]
        print(f"[{idx}] 线程 ID: {tid}")
        print(f"    有效对话轮数: {count}")
        print(f"    最近对话摘要: {summary}")
        print("-" * 50)

    print("\n🤖 测试双端自愈探针 (detect_cli_thread_update):")
    probe_empty = detect_cli_thread_update(str(ws_path), None)
    print(f"• 当飞书端会话为空时: 感知更新={probe_empty['has_update']} 目标线程={probe_empty['latest_thread_id']}")

    if len(threads) >= 2:
        oldest_tid = threads[-1]["thread_id"]
        probe_old = detect_cli_thread_update(str(ws_path), oldest_tid)
        print(f"• 当飞书端绑定旧线程 ({oldest_tid[:8]}...) 时: 感知更新={probe_old['has_update']} 切换至={probe_old['latest_thread_id'][:8]}...")


def main() -> None:
    parser = argparse.ArgumentParser(description="MyCodex 双端会话同步诊断与验证工具")
    parser.add_argument("workspace", nargs="?", default=str(REPO_ROOT), help="要诊断的工作区绝对路径")
    args = parser.parse_args()

    inspect_workspace(args.workspace)


if __name__ == "__main__":
    main()
