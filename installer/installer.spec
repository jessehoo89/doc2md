# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— doc2md 的**单文件安装程序**。

不要直接跑这个 spec：先用项目根的 `make_installer.py`（或双击 `打包安装包.bat`），
它会先把载荷 zip 放到 `build/doc2md-payload.zip`，再调用本 spec。

产物：`dist-installer/doc2md-setup.exe`
（由 make_installer.py 改名为 `doc2md-安装程序.exe` —— 构建时用 ASCII 名更稳，
中文名放在 PyInstaller 的 name 里容易在部分环境出编码问题。）

要点
----
* onefile：整个安装程序 + 内嵌载荷压成一个 exe。
* `console=False`：双击时不弹黑窗；`/S` 静默模式由 installer_app 自己在运行时
  `AttachConsole(-1)` 附加到调用方控制台。
* `excludes` 里**排掉被安装工具的那一堆重依赖**（pymupdf / onnxruntime / numpy …）：
  安装程序自己一行都用不到，不排的话安装包会从 87MB 涨到 200MB+。
* 但 **不能排 tkinter** —— 安装界面用它。
"""
import sys
from pathlib import Path

# 注意：SPECPATH 是**本 spec 文件所在目录**（installer/），不是项目根。
# 载荷在项目根的 build/ 下，所以要再往上走一级。
SPEC_DIR = Path(SPECPATH).resolve()      # noqa: F821  (SPECPATH 由 PyInstaller 注入)
ROOT = SPEC_DIR.parent

PAYLOAD = ROOT / "build" / "doc2md-payload.zip"
if not PAYLOAD.is_file():
    raise SystemExit(
        "\n[中止] 缺少内嵌载荷 build/doc2md-payload.zip\n"
        "  请先运行：python make_installer.py\n"
        "  （它会先构建 dist/doc2md，再生成载荷 zip）\n"
    )

# ---- 前置自检：安装界面要 tkinter ------------------------------------------
try:
    import tkinter  # noqa: F401
except ImportError:
    raise SystemExit(
        "\n[中止] 当前解释器没有 tkinter，安装界面打包出来会启动即崩。\n"
        f"  当前解释器：{sys.executable}\n"
        f'  请改用："{ROOT}\\.venv-gui\\Scripts\\python.exe"\n'
    )

datas = [(str(PAYLOAD), ".")]

hiddenimports = ["tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox"]

# 被安装的工具才需要的重家伙，安装程序一概不用
excludes = [
    "matplotlib", "pytest", "IPython", "pandas", "scipy", "notebook", "docutils",
    "pymupdf", "pymupdf4llm", "mammoth", "markdownify", "openpyxl", "xlrd",
    "watchdog", "numpy", "onnxruntime", "PIL",
    "win32com", "pythoncom", "pywintypes", "pywin32", "win32timezone",
    "requests", "bs4", "certifi", "charset_normalizer",
]

a = Analysis(                             # noqa: F821
    [str(ROOT / "installer" / "installer_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)                         # noqa: F821

exe = EXE(                                # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="doc2md-setup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,                        # 双击不弹黑窗；/S 由代码 AttachConsole
    disable_windowed_traceback=False,     # 崩了弹窗给 traceback，好排查
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # uac_admin=True → manifest 里写 requireAdministrator，**双击就弹 UAC**。
    #
    # 一开始走的是 asInvoker + 运行时 ShellExecuteW("runas") 自我重启，实测在真实
    # 双击场景下不可靠：用户反馈「调不出 UAC，就一直卡在那里」，右键「以管理员身份
    # 运行」才正常。原因是提权时机被推到了「点开始安装之后」，中间任何一步出岔子
    # （判断失误、窗口已销毁、UAC 框被吞）都会表现为静默卡死，且没有可用的提示。
    # 改成由 Windows 在进程启动时提权，行为可预期，也是安装程序的通行做法。
    #
    # 代价：--help、静默安装、装到用户可写目录也都要过 UAC。对"安装程序"这个形态
    # 可以接受；开发期调试请直接跑源码（installer/installer_app.py），不受此限。
    uac_admin=True,
)
