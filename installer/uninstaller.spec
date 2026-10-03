# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— doc2md 的**独立卸载程序**（单文件 exe）。

不要直接跑这个 spec：先用项目根的 `make_installer.py`（或双击 `打包安装包.bat`），
它会按 应用 → 卸载程序 → 载荷 → 安装包 的顺序编排。

构建产物：`build/uninstaller/doc2md-uninstall.exe`
再由 make_installer.py 以 `uninstall.exe` 这个名字放进载荷 zip，
最终落到安装目录，并被写进「设置 → 应用」的 UninstallString。

要点
----
* onefile：卸载程序必须**自包含**。它经常要在 ``_internal\\`` 已经被删掉一半、
  甚至整个安装目录都被删空的过程中运行，不能依赖同目录下的任何东西。
* `console=False`：双击不弹黑窗；`/S` 的文本输出由 uninstall_app 自己 tee 到
  ``%TEMP%\\doc2md-uninstall.log``。
* `uac_admin=True`：安装目录通常在有 ACL 的 Program Files 下，删除需要管理员。
  由已提权的父进程启动副本时不会二次弹 UAC。
* `excludes`：把被安装工具那一堆重依赖全排掉，不排的话这个 exe 会从 ~10MB 涨到
  100MB+。**但不能排 tkinter** —— 卸载界面要它。
"""
import sys
from pathlib import Path

# SPECPATH 是**本 spec 文件所在目录**（installer/），不是项目根
SPEC_DIR = Path(SPECPATH).resolve()      # noqa: F821  (SPECPATH 由 PyInstaller 注入)
ROOT = SPEC_DIR.parent

# ---- 前置自检：卸载界面要 tkinter ------------------------------------------
try:
    import tkinter  # noqa: F401
except ImportError:
    raise SystemExit(
        "\n[中止] 当前解释器没有 tkinter，卸载界面打出来会启动即崩。\n"
        f"  当前解释器：{sys.executable}\n"
        f'  请改用："{ROOT}\\.venv-gui\\Scripts\\python.exe"\n'
    )

hiddenimports = [
    "tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox",
    "winreg",
]

# 被卸载的那个工具才需要的重家伙，卸载程序一概不用
excludes = [
    "matplotlib", "pytest", "IPython", "pandas", "scipy", "notebook", "docutils",
    "pymupdf", "pymupdf4llm", "mammoth", "markdownify", "openpyxl", "xlrd",
    "watchdog", "numpy", "onnxruntime", "PIL",
    "win32com", "pythoncom", "pywintypes", "pywin32", "win32timezone",
    "requests", "bs4", "certifi", "charset_normalizer",
]

a = Analysis(                             # noqa: F821
    [str(SPEC_DIR / "uninstall_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
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
    name="doc2md-uninstall",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
)
