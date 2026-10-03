# -*- coding: utf-8 -*-
"""doc2md 卸载程序（独立 exe，随安装载荷释放到安装目录，名为 ``uninstall.exe``）。

由 ``installer/uninstaller.spec`` 打包；``make_installer.py`` 第 2 步产出。

谁在调它
--------
* 「设置 → 应用」/ 控制面板里的条目 —— 走注册表 ``UninstallString``
  （``<安装目录>\\uninstall.exe``；静默版 ``... /S``）
* 直接双击安装目录里的 ``uninstall.exe``
* 命令行：``uninstall.exe /S /D=<安装目录>``

为什么要有 exe（而不是沿用 ``uninstall.bat``）
---------------------------------------------
bat 双击会闪一个黑窗、控制面板点「卸载」先弹一个 cmd 黑框，也不好放进开始菜单。
exe 能给出一致的窗口体验，并且能把失败原因如实报出来（哪个文件没删掉）。

为什么要先把自己复制到 %TEMP% 再干活
------------------------------------
Windows **不允许删除正在运行的程序文件**。卸载程序自己就躺在安装目录里，它运行期间
``uninstall.exe`` 删不掉，安装目录也就删不干净。

先试过"先把除自己以外的都删掉，再交给一个游离的 ``cmd`` 循环 ``rd /s /q`` 延时重试"
—— **走不通**，实测留档：即使持有句柄的进程已经退出、同一刻在 Python 里
``os.unlink()`` 同一个文件**成功**，``rd /s /q`` 仍会一直报
「另一个程序正在使用此文件，进程无法访问」，直到循环跑满都不恢复。
（探针见 git 历史里那一轮验证，结论就是：别指望 cmd 的延时重试。）

现在的做法：启动时发现自己在安装目录里 → 把 exe 复制到
``%TEMP%\\doc2md-uninstall-<pid>.exe`` 并交给副本（副本由已提权的父进程启动，
**不会再弹一次 UAC**）→ 父进程立刻退出；由副本完成全部删除，**包括安装目录里的原
exe**。副本自己的文件用 ``MoveFileEx(..., MOVEFILE_DELAY_UNTIL_REBOOT)`` 登记到
重启时清理（它本来就在 %TEMP% 里）。

代价：``/S`` 静默卸载**返回得比较早**（交接完即返回），真正干活的是后台副本，过程写在
``%TEMP%\\doc2md-uninstall.log``。图形界面模式无感 —— 父进程不建窗口，用户只看到副本
那一个窗口。

退出码：0 成功，1 失败，2 用户取消，1223 用户在 UAC 处点了"否"。
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

APP_NAME = "doc2md"
APP_TITLE = "doc2md · 文档批量转 Markdown"
# 版本号单一来源：doc2md/__init__.py（spec 的 pathex 是仓库根，打包时会带上该包）
try:
    from doc2md import __version__ as APP_VERSION
except Exception:
    APP_VERSION = "0.0.0"

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\doc2md"
KILL_TARGETS = ("doc2md.exe", "doc2md-gui.exe")
FROM_TEMP_FLAG = "/FROM-TEMP"

USAGE = f"""{APP_TITLE} 卸载程序 {APP_VERSION}

用法：
  uninstall.exe                打开图形界面卸载（默认）
  uninstall.exe /S             静默卸载，不弹任何界面
  uninstall.exe /D=<目录>      指定安装目录（默认取本程序所在目录）
  uninstall.exe /S /D=<目录>   静默卸载指定目录
  uninstall.exe --help         显示本帮助

卸载会删除：
  * 安装目录及其**全部内容** —— 包括 config.json、.env、state.db、logs\\
    （state.db 是断点续传依据，删掉后重装需重新全量转换，请先自行备份）
  * 桌面与开始菜单里的 doc2md 快捷方式
  * 「设置 → 应用」/ 控制面板里的登记项
"""


# --------------------------------------------------------------------------- #
#  路径与小工具
# --------------------------------------------------------------------------- #
def self_exe() -> Path:
    """本程序的可执行文件路径（冻结后就是安装目录里的 uninstall.exe）。"""
    if getattr(sys, "frozen", False):
        for cand in (sys.executable, sys.argv[0] if sys.argv else ""):
            if cand and str(cand).lower().endswith(".exe"):
                p = Path(cand)
                if p.is_file():
                    return p.resolve()
    return Path(__file__).resolve()


def is_dev() -> bool:
    return not getattr(sys, "frozen", False)


def is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def can_write(d: Path) -> bool:
    """向上找第一个已存在的祖先，试试可写性（不真的建目标目录）。"""
    probe = d
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if not probe.is_dir():
        return False
    try:
        with tempfile.NamedTemporaryFile(dir=str(probe), prefix=".doc2md-wtest"):
            return True
    except OSError:
        return False


def is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def safe_stdio() -> None:
    """把不可编码的字符降级成 '?' 而不是抛 UnicodeEncodeError。

    本机控制台是 cp936；卸载文案里的「」→ 都在 GBK 里，但不排除以后加了生僻符号。
    这里**不改编码**（改成 utf-8 会让真控制台乱码），只放宽错误处理。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # type: ignore[union-attr]
        except Exception:
            pass


class _Tee:
    """输出同时给主通道与日志文件；任一失败不影响另一个。"""

    def __init__(self, primary, logfile) -> None:
        self._p = primary
        self._f = logfile

    def write(self, s: str) -> int:
        for f in (self._p, self._f):
            if f is None:
                continue
            try:
                f.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self) -> None:
        for f in (self._p, self._f):
            if f is None:
                continue
            try:
                f.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False


_LOG_PATH = Path(tempfile.gettempdir()) / "doc2md-uninstall.log"


def setup_output() -> Path:
    """窗口版构建里 ``sys.stdout`` 可能是 None；先接好输出再说话。"""
    primary = None
    if sys.stdout is not None:
        try:
            sys.stdout.fileno()
            primary = sys.stdout
        except Exception:
            primary = None
    try:
        logf = open(_LOG_PATH, "w", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        logf = None
    stream = _Tee(primary, logf)
    sys.stdout = stream
    sys.stderr = stream
    safe_stdio()
    return _LOG_PATH


def relaunch_elevated() -> int:
    """以管理员身份重启自己。

    正常用不到 —— exe 清单写的是 ``requireAdministrator``，Windows 在进程启动时就弹
    UAC。这条只是兜底（manifest 被剥离、或者源码方式运行时）。
    """
    import ctypes

    exe = sys.executable
    params = subprocess.list2cmdline(sys.argv[1:])
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    except Exception as e:  # pragma: no cover
        print(f"[错误] 提权失败：{e}")
        return 1
    if int(rc) <= 32:
        print("[错误] 用户取消了管理员授权（UAC）。")
        return 1223
    return 0


# --------------------------------------------------------------------------- #
#  删除动作
# --------------------------------------------------------------------------- #
def stop_running() -> list[str]:
    """结束正在运行的 doc2md（文件被占用会导致删不掉）。"""
    killed: list[str] = []
    for name in KILL_TARGETS:
        try:
            r = subprocess.run(
                ["taskkill", "/f", "/im", name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            killed.append(name)
    return killed


_SHORTCUT_DIRS = (
    lambda: Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    lambda: Path(os.environ.get("USERPROFILE", "")) / "Desktop",
    lambda: Path(os.environ.get("PUBLIC", "")) / "Desktop",
)


def remove_shortcuts() -> list[str]:
    """删掉 ``doc2md*.lnk``（安装程序建的都以此开头）。"""
    done: list[str] = []
    for make in _SHORTCUT_DIRS:
        try:
            d = make()
        except Exception:
            continue
        if not d.is_dir():
            continue
        n = 0
        for f in sorted(d.glob(f"{APP_NAME}*.lnk")):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
        if n:
            done.append(f"{d}（{n} 个）")
    return done


def remove_registry() -> bool:
    """删掉「设置 → 应用」里的登记项。用户级与机器级都试一遍。"""
    if os.name != "nt":
        return False
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return False
    ok = False
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            winreg.DeleteKey(hive, UNINSTALL_KEY)
            ok = True
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return ok


def unlink_force(p: Path) -> None:
    """删文件；只读的先摘掉只读位。"""
    try:
        p.unlink()
    except PermissionError:
        os.chmod(p, stat.S_IWRITE)
        p.unlink()


def rmtree_force(p: Path) -> None:
    """删目录树；失败就先把里面的只读位清一遍再删。"""
    try:
        shutil.rmtree(p)
        return
    except OSError:
        pass
    for root, _dirs, files in os.walk(p, topdown=False):
        for n in files:
            try:
                os.chmod(Path(root) / n, stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(p)


def purge_dir(root: Path, keep: set[Path] | None = None) -> tuple[list[str], list[str]]:
    """删掉 root 下除 keep 之外的一切。返回 ``(删掉的, 没删掉的)``。"""
    keep_res: set[Path] = set()
    for p in keep or ():
        try:
            keep_res.add(p.resolve())
        except OSError:
            pass

    removed: list[str] = []
    failed: list[str] = []
    if not root.is_dir():
        return removed, failed

    for p in sorted(root.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        try:
            if p.resolve() in keep_res:
                continue
        except OSError:
            pass
        try:
            if p.is_dir():
                rmtree_force(p)
            else:
                unlink_force(p)
            removed.append(p.name)
        except OSError as e:
            failed.append(f"{p.name}（{e.__class__.__name__}）")
    return removed, failed


def schedule_self_delete(p: Path) -> bool:
    """登记"下次重启时删除"。正在运行的文件删不掉，只能这样兜底。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = k32.MoveFileExW
        fn.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        fn.restype = wintypes.BOOL
        return bool(fn(str(p), None, 0x4))       # MOVEFILE_DELAY_UNTIL_REBOOT
    except Exception:
        return False


def do_uninstall(
    target: Path,
    *,
    on_step=None,
    remove_shortcut: bool = True,
    remove_registry_entry: bool = True,
) -> dict:
    """完整卸载流程。返回结果摘要。

    ``remove_shortcut`` / ``remove_registry_entry`` 是给离线测试用的开关 —— 真机上不该
    为了跑一次测试去动用户的快捷方式和注册表。
    """
    def say(msg: str) -> None:
        if on_step:
            on_step(msg)

    try:
        target = Path(target).resolve()
    except OSError:
        target = Path(target)

    # 自己的 cwd 若在目标目录里，目录本身删不掉 —— 先挪到 %TEMP%
    try:
        os.chdir(tempfile.gettempdir())
    except OSError:
        pass

    say("正在结束正在运行的 doc2md…")
    killed = stop_running()

    shortcuts: list[str] = []
    if remove_shortcut:
        say("正在删除快捷方式…")
        shortcuts = remove_shortcuts()

    registry = False
    if remove_registry_entry:
        say("正在清理「设置 → 应用」里的登记项…")
        registry = remove_registry()

    say("正在删除安装文件…")
    me = self_exe()
    keep = {me} if is_inside(me, target) else set()
    removed, failed = purge_dir(target, keep=keep)

    say("正在清理目录…")
    leftover: list[str] = []
    if target.is_dir():
        try:
            target.rmdir()
        except OSError:
            try:
                leftover = sorted(p.name for p in target.iterdir())
            except OSError:
                leftover = []

    return {
        "dir": str(target),
        "killed": killed,
        "shortcuts": shortcuts,
        "registry": registry,
        "removed": removed,
        "failed": failed,
        "leftover": leftover,
    }


# --------------------------------------------------------------------------- #
#  交接：把自己搬到 %TEMP% 再执行
# --------------------------------------------------------------------------- #
def hand_off(target: Path, *, silent: bool) -> bool:
    """把 exe 复制到 %TEMP% 并用副本接着干；返回 True 表示已交接（父进程该退出了）。

    为什么必须这么做见模块文档。三个前提：
      * 只有冻结成 exe 时才有意义（源码方式运行时脚本文件没被锁，原地干就行）；
      * 只有自己**确实躺在安装目录里**时才需要搬（别的地方运行不受影响）；
      * 副本由已提权的本进程启动，所以不会二次弹 UAC。
    """
    if is_dev():
        return False
    me = self_exe()
    if not is_inside(me, target):
        return False

    tmp = Path(tempfile.gettempdir()) / f"{APP_NAME}-uninstall-{os.getpid()}.exe"
    try:
        shutil.copy2(me, tmp)
    except OSError:
        return False

    args = [str(tmp), FROM_TEMP_FLAG, f"/D={target}"]
    if silent:
        args.append("/S")
    try:
        subprocess.Popen(args, close_fds=True, cwd=str(tempfile.gettempdir()))
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


# --------------------------------------------------------------------------- #
#  图形界面
# --------------------------------------------------------------------------- #
def run_gui(target: Path) -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    log_path = _LOG_PATH
    if not isinstance(sys.stdout, _Tee):
        log_path = setup_output()

    root = tk.Tk()
    root.title(f"{APP_TITLE} — 卸载")
    root.resizable(False, False)

    outer = ttk.Frame(root, padding=16)
    outer.pack(fill="both", expand=True)

    ttk.Label(outer, text="卸载 doc2md",
              font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
    ttk.Label(outer, text="将把下面的目录整个删掉，并清理快捷方式与「设置 → 应用」里的登记项。",
              justify="left").pack(anchor="w", pady=(8, 12))

    box = ttk.LabelFrame(outer, text=" 安装目录 ", padding=10)
    box.pack(fill="x")
    dir_var = tk.StringVar(value=str(target))
    entry = ttk.Entry(box, textvariable=dir_var, width=58, state="readonly")
    entry.pack(side="left", fill="x", expand=True)

    ttk.Label(
        outer,
        text=("注意：config.json、.env、state.db、logs\\ 都在这个目录里，会一并删除。\n"
              "state.db 是断点续传的依据，删掉后重装需要从头重转 —— 需要的话请先备份。"),
        justify="left",
        foreground="#a03a3a",
    ).pack(anchor="w", pady=(10, 0))

    bar = ttk.Progressbar(outer, mode="indeterminate", length=520)
    bar.pack(fill="x", pady=(14, 4))
    status = ttk.Label(outer, text="确认无误后点「卸载」。", anchor="w")
    status.pack(fill="x")

    btns = ttk.Frame(outer)
    btns.pack(fill="x", pady=(14, 0))
    uninstall_btn = ttk.Button(btns, text="卸载")
    uninstall_btn.pack(side="right")
    ttk.Button(btns, text="退出", command=root.destroy).pack(side="right", padx=(0, 8))

    state = {"result": None, "rc": 0}

    def set_status(msg: str) -> None:
        status.configure(text=msg)
        print(msg)                       # 交给 Tee 落日志

    def worker() -> None:
        try:
            state["result"] = do_uninstall(
                target, on_step=lambda m: root.after(0, set_status, m)
            )
            state["rc"] = 0
        except Exception as e:           # noqa: BLE001
            state["result"] = e
            state["rc"] = 1
        root.after(0, finish)

    def finish() -> None:
        bar.stop()
        res = state["result"]
        if state["rc"] != 0:
            set_status("卸载失败。")
            messagebox.showerror(
                "卸载失败",
                f"{type(res).__name__}: {res}\n\n详细日志：{log_path}",
                parent=root,
            )
            uninstall_btn.configure(state="normal")
            return

        lines = [f"已删除：{res['dir']}"]
        if res["killed"]:
            lines.append("已结束进程：" + "、".join(res["killed"]))
        if res["shortcuts"]:
            lines.append("已删除快捷方式：" + "、".join(res["shortcuts"]))
        lines.append(
            "已清理「设置 → 应用」中的登记项。"
            if res["registry"] else
            "登记项：没有找到（可能已删过）或权限不足。"
        )
        if res["failed"]:
            lines.append("")
            lines.append("以下内容未能删除：" + "、".join(res["failed"]))
            lines.append("请先关闭占用它们的程序（资源管理器/编辑器），再删一次。")
        if res["leftover"]:
            lines.append("")
            lines.append("目录里仍有残留：" + "、".join(res["leftover"]))
        lines.append("")
        lines.append(f"日志：{log_path}")
        set_status("卸载完成。")
        messagebox.showinfo("卸载完成", "\n".join(lines), parent=root)
        root.destroy()

    def start() -> None:
        if not messagebox.askyesno(
            "确认卸载",
            f"确定要卸载 doc2md 吗？\n\n"
            f"目录及其全部内容都会被删除（含配置、凭据与断点续传状态库）：\n{target}",
            parent=root, default="no",
        ):
            return
        uninstall_btn.configure(state="disabled")
        bar.start(12)
        set_status("开始卸载…")
        threading.Thread(target=worker, daemon=True).start()

    uninstall_btn.configure(command=start)
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 3
    root.geometry(f"+{x}+{y}")
    try:
        root.lift()
        root.attributes("-topmost", True)
        root.after(300, lambda: root.attributes("-topmost", False))
    except Exception:
        pass
    root.mainloop()
    return state["rc"]


def run_console(target: Path) -> int:
    print(f"{APP_TITLE} 卸载程序 {APP_VERSION}")
    print(f"安装目录：{target}")
    try:
        res = do_uninstall(target, on_step=lambda m: print("  " + m))
    except Exception as e:               # noqa: BLE001
        print(f"\n[失败] {type(e).__name__}: {e}")
        return 1

    if res["killed"]:
        print("       已结束进程：" + "、".join(res["killed"]))
    if res["shortcuts"]:
        print("       已删除快捷方式：" + "、".join(res["shortcuts"]))
    print("       登记项：" + ("已清理" if res["registry"] else "未找到或权限不足"))
    if res["failed"]:
        print("       [警告] 未能删除：" + "、".join(res["failed"]))
    if res["leftover"]:
        print("       [警告] 仍有残留：" + "、".join(res["leftover"]))
    ok = not res["failed"] and not res["leftover"]
    print(f"\n[完成] {'doc2md 已卸载。' if ok else '卸载未彻底完成，详见上面的警告。'}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
#  入口
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str]) -> dict:
    """手写解析：兼容 Inno 风格的 ``/S`` ``/D=路径``。"""
    opt = {"silent": False, "dir": None, "help": False, "from_temp": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        low = a.lower()
        if low in ("/s", "--silent", "/silent", "/verysilent"):
            opt["silent"] = True
        elif low in (FROM_TEMP_FLAG.lower(), "--from-temp"):
            opt["from_temp"] = True
        elif low in ("/norestart",):
            pass
        elif low in ("-h", "--help", "/?", "-?"):
            opt["help"] = True
        elif low.startswith("/d=") or low.startswith("--dir="):
            opt["dir"] = a.split("=", 1)[1].strip('"')
        elif low in ("/d", "--dir"):
            i += 1
            if i < len(argv):
                opt["dir"] = argv[i]
        else:
            print(f"[提示] 忽略无法识别的参数：{a}")
        i += 1
    return opt


def resolve_target(opt: dict) -> Path | None:
    """没给 ``/D=`` 时取本程序所在目录；源码方式运行时**必须显式指定**。

    源码方式下 ``self_exe()`` 是 installer/uninstall_app.py，默认目录会算成
    installer/，很容易误删东西 —— 干脆要求显式给目标。
    """
    if opt["dir"]:
        return Path(opt["dir"]).expanduser()
    if not is_dev():
        return self_exe().parent
    return None


def main() -> int:
    opt = parse_args(sys.argv[1:])

    if opt["help"]:
        setup_output()
        print(USAGE)
        return 0

    target = resolve_target(opt)
    if target is None:
        setup_output()
        print("[中止] 源码方式运行必须显式指定目标目录：\n"
              "        installer/uninstall_app.py /D=<安装目录>")
        return 1

    if not opt["silent"]:
        try:
            import tkinter  # noqa: F401
        except ImportError:
            opt["silent"] = True

    # 提权兜底（正常情况由 exe 清单在启动时就处理掉）
    if not is_admin() and not can_write(target):
        if opt["silent"]:
            setup_output()
            print(f"[信息] 没有写入权限，尝试以管理员身份重新运行：{target}")
        return relaunch_elevated()

    # 自己在安装目录里 → 交给 %TEMP% 的副本（否则删不掉自己）
    if not opt["from_temp"] and hand_off(target, silent=opt["silent"]):
        return 0

    if opt["silent"]:
        log_path = setup_output()
        print(f"[日志] {log_path}")
        code = run_console(target)
        print(f"[日志] {log_path}")
    else:
        code = run_gui(target)

    # %TEMP% 里的副本删不掉自己，登记到重启时清理（它本来就在临时目录里）
    if opt["from_temp"]:
        schedule_self_delete(self_exe())
    return code


if __name__ == "__main__":
    sys.exit(main())
