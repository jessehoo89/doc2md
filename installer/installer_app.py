# -*- coding: utf-8 -*-
"""doc2md 单文件安装程序（自解压）。

由项目根的 ``make_installer.py`` 打包成 ``dist-installer/doc2md-安装程序.exe``。
本文件是**安装程序**，不是被安装的工具本身；被安装的载荷（两个 exe + `_internal/`）
以 zip 形式内嵌在本 exe 里，运行时解到目标目录。

为什么不用 Inno Setup / NSIS：本机与外发目标都不保证装过它们。这里只用
Python 标准库 + PyInstaller，**零外部工具依赖**。

设计约定
--------
* 载荷 zip 里的内容 = 目标安装目录的内容（`doc2md.exe` / `doc2md-gui.exe` /
  `_internal/` / `README.md` / `LICENSE` / `config.example.json` / `.env.example` /
  `uninstall.exe` / `uninstall.bat`）。
* **卸载入口是 `uninstall.exe`**（注册表 `UninstallString` 指向它，图形界面）；
  `uninstall.bat` 只是兜底保留（万一 exe 被安全软件拦下）。两个都由
  「卸载程序」那套代码/脚本产出，见 `installer/uninstaller.spec`。
* 载荷里**不含** `config.json` / `.env` / `state.db`：这三样是用户数据，
  安装时只在"不存在"时补模板，升级重装不会覆盖。
* **通过 exe 清单强制提权**（manifest 里写 `requireAdministrator`，见
  `installer.spec`）：默认目录是 `C:\\Program Files\\doc2md`，双击即弹 UAC，一次到位。
  早先试过"清单用 asInvoker + 运行时按需 `ShellExecuteW("runas")` 自我提权"，
  真实双击场景下**不可靠**：提权时机被推到点「开始安装」之后，那一步一旦没成，
  表现出来就是"调不出 UAC、一直卡在那里"，且没有任何可读的错误提示。
* 装到受保护目录（Program Files / Windows）后会自动给 Users 组补「修改」权限：
  本程序运行期要把 `state.db` / `logs` / `.env` / `config.json` 写在**自己所在的
  目录**里，不补权限的话装完普通双击运行会写不进去。

命令行
------
    doc2md-安装程序.exe                     # 图形界面（默认）
    doc2md-安装程序.exe /S                  # 静默装到默认目录
    doc2md-安装程序.exe /S /D=D:\\doc2md     # 静默装到指定目录
    doc2md-安装程序.exe /S /D=... /NOICONS   # 静默且不建任何快捷方式
    doc2md-安装程序.exe --help              # 帮助

退出码：0 成功，1 失败，2 用户取消，1223 用户在 UAC 处点了"否"。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

APP_NAME = "doc2md"
APP_TITLE = "doc2md · 文档批量转 Markdown"
# 版本号单一来源：doc2md/__init__.py 的 __version__（两个 spec 的 pathex 都是仓库根，
# 打包时会把轻量的 doc2md 包一起带上，冻结后照样读得到；不再各自硬编码）
try:
    from doc2md import __version__ as APP_VERSION
except Exception:                       # 源码被单独拷走的极端情况
    APP_VERSION = "0.0.0"
PUBLISHER = "doc2md"

PAYLOAD_NAME = "doc2md-payload.zip"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\doc2md"

DEFAULT_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / APP_NAME

# UI 常显的提示文案
PITCH = (
    "把一整个目录树的 docx / doc / xls / xlsx / pdf 批量转成 Markdown，"
    "带断点续传、实时监控与扫描件云端 OCR。\n\n"
    "安装后目录里会有一份 README.md，供人阅读，也供自动化 / 智能体\n"
    "（LLM harness）直接读取以了解调用方式。"
)

USAGE = f"""{APP_TITLE} 安装程序 {APP_VERSION}

用法：
  doc2md-安装程序.exe                     打开图形界面安装（默认）
  doc2md-安装程序.exe /S                  静默安装到默认目录
  doc2md-安装程序.exe /S /D=<目录>         静默安装到指定目录
  doc2md-安装程序.exe /S /D=<目录> /NOICONS  静默安装且不建快捷方式

参数：
  /S, --silent        静默安装，不弹任何界面
  /D=<目录>           指定安装目录（也可写成 --dir <目录>）
  /NOICONS, --no-shortcuts
                      不创建桌面与开始菜单快捷方式
  /NORESTART          仅兼容保留，无实际作用
  -h, --help          显示本帮助

默认安装目录：{DEFAULT_DIR}
安装需要管理员权限：双击后会弹出 UAC 授权框，点「是」即可。
装到 Program Files 这类受保护目录时，会自动给程序目录补上"Users 可修改"权限，
这样装完普通双击运行也能正常保存配置与 state.db。
安装后可用：doc2md.exe 命令行 / doc2md-gui.exe 图形界面
"""


# --------------------------------------------------------------------------- #
#  路径与小工具
# --------------------------------------------------------------------------- #
def resource_dir() -> Path:
    """取内嵌资源所在目录（PyInstaller 单文件解开后的临时目录）。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def payload_path() -> Path:
    """定位内嵌载荷 zip；开发态回退到项目根的 build/ 目录。"""
    p = resource_dir() / PAYLOAD_NAME
    if p.is_file():
        return p
    alt = Path(__file__).resolve().parent.parent / "build" / PAYLOAD_NAME
    if alt.is_file():
        return alt
    raise FileNotFoundError(
        f"找不到内嵌载荷 {PAYLOAD_NAME}。\n"
        f"  已查找：{p}\n  以及：{alt}\n"
        "  请先运行 make_installer.py 生成载荷后再打包。"
    )


def is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def can_write(d: Path) -> bool:
    """不用真的建目录：向上找第一个已存在的祖先，试试可写性。"""
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


class _Tee:
    """把输出同时写给主通道和日志文件；任一失败都不影响另一个。"""

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


def setup_output() -> Path:
    """准备好输出通道，返回日志文件路径。

    两个坑：

    1. 本程序按 `console=False` 构建（双击时不该弹黑窗），
       windowed 构建里 ``sys.stdout`` 可能是 ``None``；被别的 GUI 程序调起时
       也可能压根没有可用句柄 —— 这时 ``print()`` 会直接抛异常。
    2. **不要**用 ``AttachConsole`` 去"借"父进程的控制台：在伪控制台（ConPTY）
       或输出管道没人读取时，往 ``CONOUT$`` 写会**永久阻塞**。实测踩过。

    所以优先级是：继承来的 stdout（重定向/管道/真 cmd 窗口都算）> 日志文件。
    无论走哪条，都同时 tee 一份到 ``%TEMP%\\doc2md-setup.log``，方便排查。
    """
    log_path = Path(tempfile.gettempdir()) / "doc2md-setup.log"

    primary = None
    if sys.stdout is not None:
        try:
            sys.stdout.fileno()          # 有真实句柄才敢用
            primary = sys.stdout
        except Exception:
            primary = None

    try:
        logf = open(log_path, "w", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        logf = None

    stream = _Tee(primary, logf)
    sys.stdout = stream
    sys.stderr = stream
    return log_path


_PROTECTED_ROOTS = (
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
    Path(os.environ.get("SystemRoot", r"C:\Windows")),
)


def _under_protected_root(d: Path) -> bool:
    for root in _PROTECTED_ROOTS:
        try:
            d.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def ensure_runtime_writable(dest: Path, on_step=None) -> bool:
    """装到 Program Files 这类受保护目录时，给 Users 组补上「修改」权限。

    为什么必须做：本程序运行期要把 ``state.db`` / ``logs\\`` / ``config.json`` /
    ``.env`` 写在**自己所在的目录**里（随身携带，换机器不用改路径）。而
    Program Files 默认只给普通用户读权限，安装又是以管理员身份跑的（文件属主是
    Administrators）—— 结果就是装完之后**普通双击运行写不进去**，表现为
    「配置保存不了、state.db 建不出来」，而且报错发生在运行期，很难联想到是安装
    权限的问题。

    只对受保护目录动手：用户自己挑的普通目录（D:\\doc2md 之类）本来就可写，
    不去额外放宽权限。
    """
    if not _under_protected_root(dest):
        return False
    if on_step:
        on_step("正在调整目录权限（让程序运行期能写自己的数据）…")
    # S-1-5-32-545 = BUILTIN\Users。用 SID 而不是组名，避开系统语言差异。
    cmd = [
        "icacls", str(dest),
        "/grant", "*S-1-5-32-545:(OI)(CI)M",
        "/T", "/C", "/Q",
    ]
    ok = False
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           errors="replace", timeout=300)
        ok = r.returncode == 0
    except Exception:
        ok = False
    if on_step:
        on_step("目录权限已调整，运行期可正常写入。"
                if ok else
                "[提示] 目录权限调整未成功；若之后出现无法保存配置，"
                "请手动给安装目录授予「修改」权限。")
    return ok


def relaunch_elevated(extra_args: list[str]) -> int:
    """以管理员身份重启自己（UAC 提权），返回子进程退出码。

    注意：正常情况下**用不到**这条路径 —— exe 的 manifest 写的是
    ``requireAdministrator``（见 installer.spec），Windows 在进程启动时就弹 UAC。
    这里只是兜底：万一 manifest 被剥离（手工重打包、杀软改写等），运行时还能自救。
    """
    import ctypes

    exe = sys.executable
    params = subprocess.list2cmdline([*sys.argv[1:], *extra_args])
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    except Exception as e:  # pragma: no cover
        print(f"[错误] 提权失败：{e}")
        return 1
    # ShellExecuteW 返回值 >32 表示成功，但拿不到子进程退出码
    if int(rc) <= 32:
        print("[错误] 用户取消了管理员授权（UAC）。")
        return 1223
    return 0


def _vbs_escape(s: str) -> str:
    return s.replace('"', '""')


def make_shortcut(lnk: Path, target: Path, workdir: Path, desc: str = "", icon: str = "") -> bool:
    """用 WScript.Shell 建 .lnk（走 VBScript，不引 pywin32，省得给安装器加依赖）。

    VBScript 文件写成 **UTF-16LE + BOM**，否则 cscript 按 ANSI 读会中文乱码。
    """
    script = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'Set lnk = sh.CreateShortcut("{_vbs_escape(str(lnk))}")\r\n'
        f'lnk.TargetPath = "{_vbs_escape(str(target))}"\r\n'
        f'lnk.WorkingDirectory = "{_vbs_escape(str(workdir))}"\r\n'
        f'lnk.Description = "{_vbs_escape(desc)}"\r\n'
        f'lnk.IconLocation = "{_vbs_escape(icon or str(target))}"\r\n'
        "lnk.Save\r\n"
    )
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".vbs")
        os.close(fd)
        with open(tmp, "wb") as f:
            f.write(b"\xff\xfe")                    # UTF-16LE BOM
            f.write(script.encode("utf-16-le"))
        lnk.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["cscript", "//nologo", "//B", tmp],
            capture_output=True, timeout=30,
        )
        return r.returncode == 0 and lnk.exists()
    except Exception:
        return False
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
#  安装核心
# --------------------------------------------------------------------------- #
def is_user_data(arc: str) -> bool:
    """用户数据：凭据 / 配置 / 状态 / 日志。安装时**已存在就不覆盖**。"""
    name = arc.rsplit("/", 1)[-1]
    if name in {".env", "config.json"} or name.startswith("state.db"):
        return True
    return arc.startswith("logs/") or "/logs/" in arc


def extract_payload(dest: Path, on_step=None) -> int:
    """把内嵌载荷解到 dest。返回解出的文件数。

    用户数据（``.env`` / ``config.json`` / ``state.db`` / ``logs/``）如果已经存在就**原样保留**，
    绝不覆盖——升级安装不能把用户配好的 token 冲掉（GUI 里也能随时改）。
    """
    zf_path = payload_path()
    dest.mkdir(parents=True, exist_ok=True)

    def say(msg: str) -> None:
        if on_step:
            on_step(msg)

    say("正在读取安装包…")
    # 不做 testzip()：那会把 150MB 全解一遍校验 CRC，等于多花一倍时间。
    # ZipFile 构造时已校验中央目录，逐条解压时也会逐条校验 CRC，够用了。
    kept = 0
    skipped: list[str] = []
    with zipfile.ZipFile(zf_path) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        total = len(members)
        say(f"正在释放 {total} 个文件…")
        for i, m in enumerate(members, 1):
            if is_user_data(m.filename):
                skipped.append(m.filename)          # 用户数据一律不进安装目录（模板由 bootstrap 生成）
            else:
                zf.extract(m, dest)
                kept += 1
            if i % 50 == 0 or i == total:
                say(f"正在释放文件… {i}/{total}")
    if skipped and on_step:
        names = "、".join(sorted({s.rsplit("/", 1)[-1] for s in skipped})[:4])
        on_step(f"已保留你原有的 {names}（安装包不覆盖用户数据）")
    return kept


def bootstrap_user_files(dest: Path, on_step=None) -> list[str]:
    """补上缺失的用户文件（配置文件 / 凭据模板），已存在的一律不动。"""
    made: list[str] = []
    pairs = [
        ("config.example.json", "config.json"),
        (".env.example", ".env"),
    ]
    for src_name, dst_name in pairs:
        src = dest / src_name
        dst = dest / dst_name
        if dst.exists() or not src.is_file():
            continue
        try:
            shutil.copyfile(src, dst)
            made.append(dst_name)
        except OSError:
            pass
    if made and on_step:
        on_step("已生成：" + "、".join(made))
    return made


def write_registry(dest: Path) -> bool:
    """写入"添加/删除程序"条目。需要管理员，失败不致命。

    卸载命令优先指向 ``uninstall.exe``（图形界面、可报错），只有载荷里没有它时才退回
    ``uninstall.bat``。用绝对路径写死：条目登记的是**这个**安装目录，不能靠 %PATH%。
    """
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return False

    unins_exe = dest / "uninstall.exe"
    unins_bat = dest / "uninstall.bat"
    unins = unins_exe if unins_exe.is_file() else unins_bat

    try:
        with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY) as k:
            def setv(name, value, typ=winreg.REG_SZ):
                winreg.SetValueEx(k, name, 0, typ, value)

            setv("DisplayName", f"{APP_NAME} 文档批量转 Markdown")
            setv("DisplayVersion", APP_VERSION)
            setv("Publisher", PUBLISHER)
            setv("InstallLocation", str(dest))
            setv("UninstallString", f'"{unins}"')
            setv("QuietUninstallString", f'"{unins}" /S')
            setv("DisplayIcon", str(dest / "doc2md-gui.exe"))
            setv("NoModify", 1, winreg.REG_DWORD)
            setv("NoRepair", 1, winreg.REG_DWORD)
            try:
                size_mb = sum(
                    p.stat().st_size for p in dest.rglob("*") if p.is_file()
                ) // 1024
                setv("EstimatedSize", size_mb, winreg.REG_DWORD)
            except OSError:
                pass
        return True
    except OSError:
        return False


def do_install(
    dest: Path,
    *,
    desktop_icon: bool = True,
    start_menu_icon: bool = True,
    on_step=None,
) -> dict:
    """完整安装流程。返回结果摘要。"""
    def say(msg: str) -> None:
        if on_step:
            on_step(msg)

    dest = dest.resolve()
    n = extract_payload(dest, on_step)
    # 必须在 bootstrap 之前：先给目录补权限，后面生成的 .env / config.json
    # 才会继承到可写的 ACL。
    ensure_runtime_writable(dest, on_step)
    made = bootstrap_user_files(dest, on_step)

    shortcuts: list[str] = []
    exe_gui = dest / "doc2md-gui.exe"
    exe_cli = dest / "doc2md.exe"
    readme = dest / "README.md"

    if desktop_icon:
        desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
        if desktop.is_dir():
            say("正在创建桌面快捷方式…")
            if make_shortcut(
                desktop / f"{APP_NAME} 图形界面.lnk", exe_gui, dest,
                APP_TITLE, str(exe_gui),
            ):
                shortcuts.append("桌面")

    if start_menu_icon:
        prog = (
            Path(os.environ.get("APPDATA", ""))
            / "Microsoft/Windows/Start Menu/Programs"
        )
        if prog.is_dir():
            say("正在创建开始菜单快捷方式…")
            if make_shortcut(
                prog / f"{APP_NAME}.lnk", exe_cli, dest, f"{APP_TITLE}（控制台菜单）",
                str(exe_cli),
            ):
                shortcuts.append("开始菜单")
            make_shortcut(
                prog / f"{APP_NAME} 图形界面.lnk", exe_gui, dest, APP_TITLE, str(exe_gui)
            )

    say("正在登记卸载信息…")
    registry = write_registry(dest)

    return {
        "dir": str(dest),
        "files": n,
        "made": made,
        "shortcuts": shortcuts,
        "registry": registry,
        "readme": str(readme),
    }


# --------------------------------------------------------------------------- #
#  图形界面
# --------------------------------------------------------------------------- #
def run_gui(default_dir: Path, no_shortcuts: bool) -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    # GUI 模式下 sys.stdout 常常是 None；先接好输出，免得任何一句 print 炸掉整个安装
    log_path = Path(tempfile.gettempdir()) / "doc2md-setup.log"
    if not isinstance(sys.stdout, _Tee):
        log_path = setup_output()

    root = tk.Tk()
    root.title(f"{APP_TITLE} — 安装程序")
    root.resizable(False, False)

    try:
        root.iconbitmap(default="")
    except Exception:
        pass

    outer = ttk.Frame(root, padding=16)
    outer.pack(fill="both", expand=True)

    ttk.Label(outer, text="安装 doc2md", font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
    ttk.Label(outer, text=PITCH, justify="left").pack(anchor="w", pady=(8, 12))

    box = ttk.LabelFrame(outer, text=" 安装目录 ", padding=10)
    box.pack(fill="x")
    dir_var = tk.StringVar(value=str(default_dir))
    entry = ttk.Entry(box, textvariable=dir_var, width=58)
    entry.pack(side="left", fill="x", expand=True)

    def browse() -> None:
        picked = filedialog.askdirectory(
            title="选择安装目录", initialdir=str(default_dir)
        )
        if picked:
            dir_var.set(os.path.normpath(picked))

    ttk.Button(box, text="浏览…", command=browse).pack(side="left", padx=(8, 0))

    opts = ttk.Frame(outer)
    opts.pack(fill="x", pady=(10, 0))
    desk_var = tk.BooleanVar(value=not no_shortcuts)
    menu_var = tk.BooleanVar(value=not no_shortcuts)
    ttk.Checkbutton(opts, text="创建桌面快捷方式", variable=desk_var).pack(side="left")
    ttk.Checkbutton(opts, text="创建开始菜单快捷方式", variable=menu_var).pack(side="left", padx=(16, 0))

    note = ttk.Label(
        outer,
        text=(
            "提示：装到 C:\\Program Files 需要管理员权限，安装时会弹一次 UAC 授权；\n"
            "装到 D 盘等自己有写权限的目录则不需要。"
        ),
        justify="left",
        foreground="#666666",
    )
    note.pack(anchor="w", pady=(12, 0))

    bar = ttk.Progressbar(outer, mode="indeterminate", length=520)
    bar.pack(fill="x", pady=(14, 4))
    status = ttk.Label(outer, text="准备就绪。", anchor="w")
    status.pack(fill="x")

    btns = ttk.Frame(outer)
    btns.pack(fill="x", pady=(14, 0))
    install_btn = ttk.Button(btns, text="开始安装")
    install_btn.pack(side="right")
    open_btn = ttk.Button(btns, text="打开安装目录", state="disabled")
    open_btn.pack(side="right", padx=(0, 8))
    readme_btn = ttk.Button(btns, text="查看 README", state="disabled")
    readme_btn.pack(side="right", padx=(0, 8))
    ttk.Button(btns, text="退出", command=root.destroy).pack(side="right", padx=(0, 8))

    state = {"result": None, "rc": 0}


    def set_status(msg: str) -> None:
        status.configure(text=msg)
        print(msg)          # 交给 Tee 落日志，别自己再开一个写句柄

    def worker(target: Path) -> None:
        try:
            res = do_install(
                target,
                desktop_icon=desk_var.get(),
                start_menu_icon=menu_var.get(),
                on_step=lambda m: root.after(0, set_status, m),
            )
            state["result"] = res
            state["rc"] = 0
        except Exception as e:  # noqa: BLE001
            state["result"] = e
            state["rc"] = 1
        root.after(0, finish)

    def finish() -> None:
        bar.stop()
        res = state["result"]
        if state["rc"] != 0:
            set_status("安装失败。")
            messagebox.showerror(
                "安装失败",
                f"{type(res).__name__}: {res}\n\n详细日志：{log_path}",
                parent=root,
            )
            install_btn.configure(state="normal")
            return

        set_status(f"安装完成，共 {res['files']} 个文件。")
        extra = []
        if res["shortcuts"]:
            extra.append("已创建快捷方式：" + "、".join(res["shortcuts"]))
        if res["made"]:
            extra.append("已生成：" + "、".join(res["made"]))
        if not res["registry"]:
            extra.append("（未能写入卸载登记，可能需要管理员权限）")
        # 刻意不写"下一步 1/2/3"：配置与云端 OCR Token 现在都在图形界面的
        # 「设置…」窗口里，让用户去手改 config.json / .env 反而是绕远路。
        messagebox.showinfo(
            "安装完成",
            f"已安装到：\n{res['dir']}\n\n"
            f"{os.linesep.join(extra)}\n\n"
            "双击 doc2md-gui.exe 打开图形界面；配置与云端 OCR Token 都在\n"
            "界面里的「设置…」（Ctrl+,）中修改。\n\n"
            f"说明文档：{res['readme']}\n"
            f"卸载：{res['dir']}\\uninstall.exe",
            parent=root,
        )
        install_btn.configure(state="disabled")
        open_btn.configure(state="normal")
        readme_btn.configure(state="normal")

        def open_dir() -> None:
            os.startfile(res["dir"])  # type: ignore[attr-defined]

        def open_readme() -> None:
            os.startfile(res["readme"])  # type: ignore[attr-defined]

        open_btn.configure(command=open_dir)
        readme_btn.configure(command=open_readme)

    def start() -> None:
        try:
            target = Path(dir_var.get()).expanduser()
        except Exception:
            messagebox.showerror("目录无效", "请填写一个合法的安装目录。", parent=root)
            return
        if not dir_var.get().strip():
            messagebox.showerror("目录无效", "安装目录不能为空。", parent=root)
            return

        if not can_write(target) and not is_admin():
            ok = messagebox.askyesno(
                "需要管理员权限",
                f"当前用户没有写入权限：\n{target}\n\n"
                "是否以管理员身份重新运行安装程序？",
                parent=root,
            )
            if not ok:
                return
            root.destroy()
            code = relaunch_elevated([f"/D={target}"])
            state["rc"] = code
            return

        install_btn.configure(state="disabled")
        bar.start(12)
        set_status("开始安装…")
        threading.Thread(target=worker, args=(target,), daemon=True).start()

    install_btn.configure(command=start)
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 3
    root.geometry(f"+{x}+{y}")
    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    root.mainloop()
    return state["rc"]


def run_console(dest: Path, desktop_icon: bool, start_menu_icon: bool) -> int:
    print(f"{APP_TITLE} 安装程序 {APP_VERSION}")
    print(f"安装目录：{dest}")
    try:
        res = do_install(
            dest, desktop_icon=desktop_icon, start_menu_icon=start_menu_icon,
            on_step=lambda m: print("  " + m),
        )
    except Exception as e:  # noqa: BLE001
        print(f"\n[失败] {type(e).__name__}: {e}")
        return 1
    print(f"\n[完成] 共释放 {res['files']} 个文件到 {res['dir']}")
    if res["shortcuts"]:
        print("       快捷方式：" + "、".join(res["shortcuts"]))
    if res["made"]:
        print("       已生成：" + "、".join(res["made"]))
    print(f"       说明文档：{res['readme']}")
    return 0


# --------------------------------------------------------------------------- #
#  入口
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str]) -> tuple[dict, Path]:
    """手写解析：要兼容 Inno 风格的 `/S` `/D=路径` `/NOICONS`。"""
    opt = {"silent": False, "dir": None, "shortcuts": True, "help": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        low = a.lower()
        if low in ("/s", "--silent", "/silent", "/verysilent"):
            opt["silent"] = True
        elif low in ("/noicons", "--no-shortcuts", "/noshortcuts"):
            opt["shortcuts"] = False
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

    dest = Path(opt["dir"]) if opt["dir"] else DEFAULT_DIR
    return opt, dest


def main() -> int:
    opt, dest = parse_args(sys.argv[1:])

    if opt["help"]:
        setup_output()
        print(USAGE)
        return 0

    if not opt["silent"]:
        try:
            import tkinter  # noqa: F401
        except ImportError:
            opt["silent"] = True

    if opt["silent"]:
        log_path = setup_output()
        if not can_write(dest) and not is_admin():
            print(f"[信息] 没有写入权限，尝试以管理员身份重新运行：{dest}")
            return relaunch_elevated([f"/D={dest}"])
        print(f"[日志] {log_path}")
        code = run_console(dest, opt["shortcuts"], opt["shortcuts"])
        print(f"[日志] {log_path}")
        return code

    return run_gui(dest, no_shortcuts=not opt["shortcuts"])


if __name__ == "__main__":
    sys.exit(main())
