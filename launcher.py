"""交互式启动器：双击 bat 后显示中文菜单，选择要执行的操作。

.bat 只含纯 ASCII，中文提示全部由本脚本输出，避免 cmd 代码页乱码。

菜单项 G 会切到图形界面（gui.py），两者共用同一套核心，功能完全一样；
图形界面需要带 tkinter 的解释器 —— 项目根的 .venv-gui 就是为此准备的。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TOOL_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)     # 打包后：exe 所在目录（config.json/.env 就在旁边）
    else Path(__file__).resolve().parent
)
sys.path.insert(0, str(TOOL_DIR))

MENU = """
================================================================================
                        文档批量转 Markdown
================================================================================
  把 docx / doc / xls / xlsx / pdf 转成 Markdown，存回原目录同名 .md
  扫描件（无文字层）自动调用 PaddleOCR 云端识别
  敏感目录（个人资料等）已配置为不上传云端

    [G]  打开图形界面 ⭐         窗口版操作界面（按钮点选，功能与本菜单完全一样）
    [1]  扫描 / 试运行          看看有多少文件、走哪条通道（不写任何文件）
    [2]  开始批量转换            全量转换，中断后重跑会自动续传
    [3]  启动实时监控            常驻监控新增和修改的文件，自动转换
    [4]  查看转换统计            已转多少、失败多少、今日 OCR 用了多少页
    [5]  重试失败的文件          只重跑上次失败的
    [6]  检测云端 OCR 状态       后端连通性 + Token 是否生效（配好后先跑这个）
    [7]  打开配置文件            config.json（目录、排除、敏感词、并发等）
    [8]  打开仓库根目录          程序与配置都在这里
    [9]  打开凭据文件            .env（PaddleOCR / MinerU / 硅基流动 的 Token 填这里）
    [0]  退出
================================================================================
"""

# 凭据文件不存在时自动落一份模板（内容与仓库根的 .env.example 一致）
_ENV_TEMPLATE = """# 云端 OCR 凭据文件（改完需重启程序；取值优先级：系统环境变量 > 本文件 > config.json）
#
# MinerU —— https://mineru.net 登录后到「API 管理」创建 Token。
#   填上后 precision 精度解析可用（≤200MB/200 页，会返回插图）；
#   留空也不报错：MinerU 轻量接口免 Token，但只出文字、不回插图。
DOC2MD_MINERU_TOKEN=

# PaddleOCR —— 星河社区 aistudio 访问令牌（默认优先级最高的后端）
PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN=

# 硅基流动 —— https://siliconflow.cn 控制台「API 密钥」页创建。
#   填上后 sf-deepseek-ocr（专用 OCR 模型，PDF 直传）即可用。注意它不返回插图。
DOC2MD_SILICONFLOW_TOKEN=
"""


def _cfg_path() -> Path:
    return TOOL_DIR / "config.json"


def _env_path() -> Path:
    return TOOL_DIR / ".env"


def _open_credentials() -> None:
    """打开凭据文件 .env；不存在则先写一份模板，避免用户对着空目录发愁。"""
    p = _env_path()
    if not p.exists():
        try:
            p.write_text(_ENV_TEMPLATE, encoding="utf-8")
            print(f"\n已生成凭据文件模板：{p}")
            print("请把各平台的 Token 填在等号右侧，保存后重启本工具即生效。")
        except OSError as e:
            print(f"\n[错误] 无法创建 {p}：{e}")
            return
    print(f"\n凭据文件：{p}")
    print("填写方法：Token 直接跟在等号后面，不要加空格；以 # 开头的行是注释。")
    print("若没有自动打开，请右键该文件 → 打开方式 → 记事本。")
    _open_path(p, editor_fallback=True)


def _run(args: list[str], *, pause: bool = True) -> None:
    """执行一个子命令。

    源码运行时起独立子进程（干净、Ctrl+C 不会带走菜单）。
    **打包后必须改为同进程直接调用**：那时 sys.executable 就是本 exe 自己，
    再拼 `[exe, "-m", "doc2md", ...]` 会变成无限自我递归。
    """
    print()
    if getattr(sys, "frozen", False):
        try:
            from doc2md.cli import main as cli_main

            cli_main(args)
        except KeyboardInterrupt:
            print("\n已中断。")
            return
        except SystemExit:
            pass
        if pause:
            input("\n按回车返回菜单…")
        return

    cmd = [sys.executable, "-m", "doc2md", *args]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(TOOL_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        subprocess.run(cmd, cwd=str(TOOL_DIR), env=env, check=False)
    except KeyboardInterrupt:
        print("\n已中断。")
        return
    if pause:
        input("\n按回车返回菜单…")


def _open_path(p: Path, *, editor_fallback: bool = False) -> None:
    try:
        os.startfile(str(p))  # type: ignore[attr-defined]
        return
    except OSError:
        pass
    if editor_fallback:
        # .env 没有文件关联时（双击会弹"选择打开方式"），直接叫记事本
        try:
            subprocess.run(["notepad", str(p)], check=False)
            return
        except OSError:
            pass
    subprocess.run(["cmd", "/c", "start", "", str(p)], check=False)


def _open_gui() -> None:
    """切到图形界面（同一个进程内起 Tk 主循环，退出后回到菜单）。

    这里要单独判一次 tkinter：本机的 WorkBuddy 托管 Python 是精简版，**没有
    tkinter**，直接 import 会抛 ModuleNotFoundError。与其让用户看到一个
    traceback，不如明确告诉他该用哪个解释器。
    """
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("\n[错误] 当前 Python 解释器没有 tkinter，无法打开图形界面。")
        print(f"       当前解释器：{sys.executable}")
        print("       请改用带 tkinter 的解释器（项目根下的 .venv-gui 就是）：")
        print(f'         "{TOOL_DIR}\\.venv-gui\\Scripts\\python.exe" gui.py')
        print("       或者直接双击 文档转MD-GUI.bat，它自己会挑对解释器。")
        input("\n按回车返回菜单…")
        return
    print("\n正在打开图形界面…关闭窗口即可返回本菜单。")
    try:
        from gui import main as gui_main

        gui_main()
    except Exception as e:
        print(f"\n[错误] 图形界面启动失败：{type(e).__name__}: {e}")
        print("       可以改用菜单项 1-9 继续操作，功能完全一样。")
        input("\n按回车返回菜单…")


def main() -> int:
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print(MENU)
        try:
            choice = input("请输入序号后回车：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice in ("g", "G"):
            _open_gui()
        elif choice == "1":
            _run(["scan"])
        elif choice == "2":
            _run(["run"])
        elif choice == "3":
            print("\n监控模式会一直运行，关闭本窗口即停止。")
            _run(["watch"])
        elif choice == "4":
            _run(["status"])
        elif choice == "5":
            _run(["retry"])
        elif choice == "6":
            _run(["ping"], pause=False)
            _run(["env"])
        elif choice == "7":
            _open_path(_cfg_path())
        elif choice == "8":
            _open_path(TOOL_DIR)
        elif choice == "9":
            _open_credentials()
            input("\n按回车返回菜单…")
        elif choice == "0":
            print("\n再见。")
            return 0
        else:
            print("\n[提示] 请输入 0-9 之间的序号，或 G 打开图形界面。")
            input("按回车继续…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
