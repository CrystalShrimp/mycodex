#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MyCodex 跨平台交互配置向导。
负责：工作空间确认、环境自检（Node.js / Codex CLI）、ChatGPT 授权与全功能配置中心（飞书/企微/白名单）。
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

# 确保 Windows 下控制台 Unicode 安全输出
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleCP(65001)
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        if sys.stdin and hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT_DIR = Path(__file__).resolve().parent.parent


def run_cmd(cmd: list[str], cwd: Path | None = None, check: bool = False, env: dict | None = None) -> int:
    """运行子命令并实时透传标准输入输出。"""
    c_env = os.environ.copy()
    if env:
        c_env.update(env)
    # 在 Windows 下如果命令是 npm 或 npx，需开启 shell=True 或解析为 npm.cmd
    use_shell = sys.platform == "win32" and cmd[0] in ("npm", "npx")
    res = subprocess.run(cmd, cwd=cwd or ROOT_DIR, env=c_env, shell=use_shell)
    if check and res.returncode != 0:
        sys.exit(res.returncode)
    return res.returncode


def get_env_value(key: str) -> str:
    """读取 .env 中的指定配置项。"""
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return ""
    try:
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip("'\"")
    except Exception:
        pass
    return ""


def upsert_env_key(key: str, value: str):
    """更新或插入 .env 文件中的配置项。"""
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        example_file = ROOT_DIR / "config" / "env.example"
        if example_file.exists():
            shutil.copy(example_file, env_file)
        else:
            env_file.write_text(f"{key}={value}\n", encoding="utf-8")
            return

    try:
        lines = env_file.read_text(encoding="utf-8", errors="replace").splitlines()
        found = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("#") and "=" in stripped:
                k, _ = stripped.split("=", 1)
                if k.strip() == key:
                    new_lines.append(f"{key}={value}")
                    found = True
                    continue
            new_lines.append(line)

        if not found:
            new_lines.append(f"{key}={value}")

        env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception as e:
        print(f"[!] 写入 .env 失败: {e}")


def get_recommended_workspace() -> Path:
    """获取跨平台智能推荐的工作空间目录。"""
    if sys.platform != "win32":
        return Path.home() / "projects"
    # Windows: 如果存在 D 盘，推荐 D:\projects，否则推荐 C:\projects
    if Path("D:\\").exists():
        return Path("D:\\projects")
    return Path("C:\\projects")


def confirm_workspaces():
    """【Step 1/4】运行目录与工作空间确认 (Workspace)。"""
    print("\n" + "=" * 60)
    print("      【Step 1/4】运行目录与工作空间确认 (Workspace)")
    print("=" * 60)
    print()
    print("  [1] MyCodex 程序运行目录 (只读):")
    print(f"      {ROOT_DIR.resolve()}")
    print("      >> 说明: 用于承载网关服务、系统核心配置（.env）与运行日志。\n")

    # 1. 确保 .env 基础文件存在
    env_file = ROOT_DIR / ".env"
    example_file = ROOT_DIR / "config" / "env.example"
    if not env_file.exists():
        if example_file.exists():
            shutil.copy(example_file, env_file)
            print("[OK] 已初始化生成 .env 配置文件。")
        else:
            env_file.touch()

    # 2. 获取当前已配置或智能推荐路径
    configured = get_env_value("DEFAULT_WORKSPACE")
    if configured:
        rec_path = Path(configured).expanduser()
    else:
        rec_path = get_recommended_workspace()

    print("  [2] Codex 默认工作空间 (AI 操盘区):")
    print(f"      当前推荐: {rec_path}")
    print("      >> 说明: AI 编写业务代码、读取项目、执行终端指令的实际工程文件夹。")
    print()

    choice = input("[?] 是否直接采用此工作目录？[Y/N] (直接回车 = 推荐路径): ").strip().lower()
    final_ws = rec_path
    if choice == "n":
        while True:
            custom_input = input("请输入您希望 AI 操盘的代码工程目录绝对路径: ").strip().strip("'\"")
            if not custom_input:
                print("[!] 路径不能为空，请重新输入。")
                continue
            try:
                final_ws = Path(custom_input).expanduser().resolve()
                break
            except Exception as e:
                print(f"[!] 路径格式无效 ({e})，请重新输入。")

    # 自动在磁盘创建目标目录
    try:
        final_ws.mkdir(parents=True, exist_ok=True)
        print(f"[OK] 默认工作空间已就绪: {final_ws}")
    except Exception as e:
        print(f"[!] 创建目录遇到问题 ({e})，但已记录该路径。")

    # 写入 .env 文件
    upsert_env_key("DEFAULT_WORKSPACE", str(final_ws))


def check_environment():
    """【Step 2/4】基础运行环境检测 (Environment)。"""
    print("\n" + "=" * 60)
    print("          【Step 2/4】运行环境自检与依赖 (Environment)")
    print("=" * 60)
    print()

    # 1. 检查 Node.js
    node_path = shutil.which("node")
    if not node_path:
        print("[!] 警告: 未检测到 Node.js 环境（auto_feishu 需要 Node.js v20+）。")
        print("    建议前往 https://nodejs.org 下载安装 Node.js LTS 版本。")
    else:
        try:
            ver = subprocess.check_output(["node", "-v"], text=True).strip()
            print(f"[OK] Node.js 运行时环境: {ver}")
        except Exception:
            print("[OK] 检测到 Node.js 运行时。")

    # 2. 检查 Codex CLI
    codex_path = shutil.which("codex")
    if not codex_path:
        print("[!] 提示: 未检测到全局 Codex CLI。")
        choice = input("[?] 是否立即通过国内镜像全局安装 Codex CLI？[Y/N] (默认 N): ").strip().lower()
        if choice == "y":
            print("[*] 正在安装 @openai/codex ...")
            run_cmd(["npm", "install", "-g", "@openai/codex", "--registry=https://registry.npmmirror.com"])
    else:
        print("[OK] Codex CLI 已就绪。")


def check_or_setup_models():
    """【Step 3/4】Codex 认证状态检查 (Codex Auth)。"""
    print("\n" + "=" * 60)
    print("      【Step 3/4】Codex 账号认证状态 (Codex Auth)")
    print("=" * 60)
    print()

    auth_file = Path.home() / ".codex" / "auth.json"
    has_auth = auth_file.exists() and auth_file.stat().st_size > 10

    if has_auth:
        print("[OK] Codex ChatGPT 登录态已就绪 (~/.codex/auth.json)")
        c = input("[?] 是否需要重新登录或切换 ChatGPT 账号？[y/N] (直接回车 = 保持当前): ").strip().lower()
        if c == "y":
            run_cmd(["codex", "login"])
    else:
        print("[!] 提示: 未检测到 Codex ChatGPT 登录态。")
        print("    MyCodex 运行依赖本机 ChatGPT 登录授权（无需填 API Key）。")
        c = input("[?] 是否立即在终端执行 'codex login' 进行登录？[Y/N] (直接回车 = 是): ").strip().lower()
        if c in ("", "y"):
            run_cmd(["codex", "login"])


def ensure_playwright_chromium(auto_feishu_dir: Path) -> bool:
    """快速检测 Playwright Chromium 驱动，已就绪则秒级跳过，未就绪则镜像加速安装。"""
    # 1. 尝试检测 Chromium 可执行文件是否已存在
    check_script = "const { chromium } = require('playwright'); const p = chromium.executablePath(); process.exit(require('fs').existsSync(p) ? 0 : 1);"
    try:
        res = subprocess.run(["node", "-e", check_script], cwd=auto_feishu_dir, capture_output=True, text=True)
        if res.returncode == 0:
            print("[OK] Playwright Chromium 浏览器驱动已就绪。")
            return True
    except Exception:
        pass

    # 2. 若未就绪，清理潜在的残留死锁目录 __dirlock
    try:
        cache_dirs = []
        if sys.platform == "darwin":
            cache_dirs.append(Path.home() / "Library" / "Caches" / "ms-playwright")
        elif sys.platform == "win32":
            local_appdata = os.environ.get("LOCALAPPDATA")
            if local_appdata:
                cache_dirs.append(Path(local_appdata) / "ms-playwright")
        else:
            cache_dirs.append(Path.home() / ".cache" / "ms-playwright")

        for cd in cache_dirs:
            dirlock = cd / "__dirlock"
            if dirlock.exists():
                shutil.rmtree(dirlock, ignore_errors=True)
    except Exception:
        pass

    # 3. 注入国内镜像加速执行安装
    print("[*] 正在安装 Playwright Chromium 组件（已启用国内镜像加速）...")
    install_env = {
        "PLAYWRIGHT_DOWNLOAD_HOST": os.environ.get("PLAYWRIGHT_DOWNLOAD_HOST", "https://npmmirror.com/mirrors/playwright")
    }
    code = run_cmd(["npx", "playwright", "install", "chromium"], cwd=auto_feishu_dir, env=install_env)
    return code == 0


def setup_feishu(mode: str):
    """直接使用 npm 直驱执行 auto_feishu 自动化，无中间层，杜绝二次询问与参数丢失。"""
    auto_feishu_dir = ROOT_DIR / "auto_feishu"
    if not auto_feishu_dir.exists():
        print("[ERROR] 未找到 auto_feishu 自动化目录。")
        return

    mode_label = "个人用" if mode == "personal" else "公用"
    print(f"\n[*] 准备飞书自动化配置环境（已选定: {mode_label} 模式）...")

    # 1. 确保 auto_feishu npm 依赖
    node_modules = auto_feishu_dir / "node_modules"
    playwright_pkg = node_modules / "playwright"
    if not node_modules.exists() or not playwright_pkg.exists():
        print("[*] 依赖缺失或未完全安装，正在安装依赖 (npm install)...")
        run_cmd(["npm", "install", "--no-audit", "--no-fund"], cwd=auto_feishu_dir)

    # 2. 确保 Playwright Chromium 浏览器组件（已就绪秒级跳过，未就绪镜像加速）
    ensure_playwright_chromium(auto_feishu_dir)

    # 3. 按选定模式直驱飞书配置脚本
    print(f"[*] 正在执行飞书自动化配置（{mode_label}）...")
    custom_env = {"FEISHU_DEPLOY_MODE": mode}
    npm_cmd = ["npm", "run", "feishu:setup"]
    if mode == "personal":
        npm_cmd.extend(["--", "--personal"])

    code = run_cmd(npm_cmd, cwd=auto_feishu_dir, env=custom_env)
    if code == 0:
        print(f"[OK] 飞书{mode_label}模式自动化配置完成。")
    else:
        print(f"[!] 飞书自动化配置退出，返回码: {code}")


def ask_group_import():
    """飞书公用模式下的白名单导入交互。"""
    print("\n" + "=" * 46)
    print("          飞书公用模式 - 白名单配置")
    print("=" * 46)
    print(">> 默认模式：ALLOWED_USERS 保持为空，企业内全员均可直接访问。")
    print()
    choice = input("[?] 是否需要将特定群聊的所有用户ID一键导入为白名单？[Y/N] (默认 N): ").strip().lower()
    if choice == "y":
        run_cmd([sys.executable, str(ROOT_DIR / "scripts" / "import_feishu_group.py")])
    else:
        from scripts.import_feishu_group import upsert_env
        upsert_env("ALLOWED_USERS", "")
        print("[OK] 已将 ALLOWED_USERS 设为全员开放模式。")


def configure_autostart():
    """配置/管理开机自启（每次开机自动静默后台运行）。"""
    if sys.platform == "win32":
        autostart_script = ROOT_DIR / "scripts" / "setup_autostart.bat"
        if not autostart_script.exists():
            print("[!] 未找到 scripts/setup_autostart.bat。")
            return
        run_cmd(["cmd.exe", "/c", str(autostart_script)])
    else:
        autostart_script = ROOT_DIR / "scripts" / "setup_autostart_mac.sh"
        if not autostart_script.exists():
            print("[!] 未找到 scripts/setup_autostart_mac.sh。")
            return
        run_cmd(["bash", str(autostart_script)])


def main_menu():
    """【Step 4/4】消息平台接入与配置中心。"""
    while True:
        print("\n" + "=" * 60)
        print("    【Step 4/4】消息平台接入与配置中心 (Platform Access)")
        print("=" * 60)
        print("\n【飞书接入】")
        print(" 1. 配置飞书 - 个人用（仅创建者可用，不改变应用可用范围）")
        print(" 2. 配置飞书 - 公用（可用范围全员，支持群成员一键导入白名单）")
        print(" 3. 飞书群成员一键导入白名单（日常维护工具）")
        print("\n【企业微信接入】")
        print(" 4. 配置企业微信（长连接自建应用 + 连通实测）")
        print("\n【组合与授权管理】")
        print(" 5. 完整配置（飞书公用 + 企业微信）")
        print(" 6. Codex 认证管理（登录/切换 ChatGPT 账号）")
        print("\n【系统】")
        print(" 7. 配置开机自启（每次开机自动静默后台运行）")
        print("\n【退出】")
        print(" 0. 退出向导（完成并显示启动说明）")
        print()

        try:
            choice = input("请选择 [0/1/2/3/4/5/6/7]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n已退出。")
            break

        if choice == "1":
            setup_feishu("personal")
        elif choice == "2":
            setup_feishu("public")
            ask_group_import()
        elif choice == "3":
            run_cmd([sys.executable, str(ROOT_DIR / "scripts" / "import_feishu_group.py")])
        elif choice == "4":
            run_cmd([sys.executable, str(ROOT_DIR / "scripts" / "setup_wecom.py")])
        elif choice == "5":
            setup_feishu("public")
            ask_group_import()
            print("\n[*] 接下来进入企业微信配置...")
            run_cmd([sys.executable, str(ROOT_DIR / "scripts" / "setup_wecom.py")])
        elif choice == "6":
            run_cmd(["codex", "login"])
        elif choice == "7":
            configure_autostart()
        elif choice in ("0", "q", "exit"):
            finish_setup()
            break
        else:
            print("[!] 无效选项，请重新输入。")


def finish_setup():
    """完成向导并展示运行说明。"""
    print("\n" + "=" * 50)
    print("[SUCCESS] MyCodex 安装与配置完成！")
    print("=" * 50)
    if sys.platform == "win32":
        print("  启动服务      : 双击 launcher_windows\\MyCodex.bat（重启用 launcher_windows\\MyCodex-Restart.bat）")
        print("  重新配置      : 双击 launcher_windows\\MyCodex-Setup.bat 重跑向导")
    else:
        print("  启动服务      : 双击 launcher_macos/MyCodex.command（或运行 bash scripts/restart_mac.sh）")
        print("  重新配置      : 双击 launcher_macos/MyCodex-Setup.command 重跑向导")
    print("  开机自启      : 配置中心选 7")
    print("  健康检查      : curl http://127.0.0.1:8090/health")
    print("=" * 50)


if __name__ == "__main__":
    os.chdir(ROOT_DIR)
    # 4 步极简引导流
    confirm_workspaces()
    check_environment()
    check_or_setup_models()
    main_menu()

