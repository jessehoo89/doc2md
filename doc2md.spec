# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— doc2md（文档批量转 Markdown）。

用法（在项目根，用**带 tkinter 的那个解释器**）：
    Windows:  .venv-gui\Scripts\python.exe -m PyInstaller doc2md.spec --noconfirm
              或直接双击 打包EXE.bat（它会自动优先挑 .venv-gui）
    Linux:    bash build_linux.sh   （.venv/bin/python -m PyInstaller doc2md.spec --noconfirm）
              解释器没有 tkinter 时只打控制台版 doc2md，跳过窗口版

单文件形态（便于分发：一个可执行文件拷走即用）：
    DOC2MD_ONEFILE=1 python -m PyInstaller doc2md.spec --noconfirm --distpath dist-onefile
    或  bash build_linux.sh onefile
    产物 dist-onefile/doc2md 是一个自解压可执行文件；代价是每次启动要把内置
    资源解包到临时目录（慢若干秒、占 /tmp），故只出控制台版。

产物：dist/doc2md/ 整个文件夹，里面有两个 exe：
    doc2md.exe       控制台版 —— 双击进中文菜单，带参数等价于 python -m doc2md
    doc2md-gui.exe   窗口版   —— 双击直接开图形界面

两者**共享同一个 _internal 目录**，所以多带一个窗口版只多几 MB，
不是把 130 多 MB 再复制一份。原理：同一份 Analysis/PYZ 造两个 EXE 对象，
再一起 COLLECT 进同一个目录。

!!! 重要 !!!
  必须用带 tkinter 的解释器打包。本机 WorkBuddy 托管的 Python 3.13 是精简版，
  没有 tkinter（连 _tkinter.pyd 都没有），用它打包出来的窗口版一启动就崩 ——
  而且打包时不会报错，只在运行时才炸。所以本 spec 会做一次前置自检（见下）。

分发要点：
  1. 整个 dist/doc2md 文件夹一起拷走，不能只拷 exe（依赖在 _internal 里）。
  2. config.json / .env 放在 exe **旁边**（首次运行会自动生成配置模板）。
     程序根目录按 exe 位置解析，所以配置和 state.db 会跟着程序走、不会丢。
  3. .doc / .xls / .wps / .et 仍走本机 WPS/Office 的 COM，**目标机必须装**；
     本地 RapidOCR 也仍需那套独立解释器（config.json 里 local_ocr.python_exe）。
     这两项打不进去，属能力边界而非缺陷。
"""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve()          # noqa: F821  (SPECPATH 由 PyInstaller 注入)

# ---- 前置自检：没有 tkinter 就别往下走了，早报错好过运行时才炸 ---------------
HAS_TK = True
if sys.version_info >= (3, 11):
    try:
        import tkinter  # noqa: F401
    except ImportError:
        HAS_TK = False
        if sys.platform == "win32":
            raise SystemExit(
                "\n[打包中止] 当前解释器没有 tkinter，窗口版打包出来会启动即崩。\n"
                f"  当前解释器：{sys.executable}\n"
                "  改用带 tkinter 的解释器，例如：\n"
                f'    "{ROOT}\\.venv-gui\\Scripts\\python.exe" -m PyInstaller doc2md.spec --noconfirm\n'
                "  （项目自带的 .venv 是精简 Python，没有 tkinter）\n"
            )
        # Linux/macOS：命令行是主用法，缺 tkinter 只跳过窗口版，不阻断打包。
        # 想连窗口版一起打：apt install python3-tk（或 brew install python-tk）。
        print("[提示] 解释器没有 tkinter，本次只打控制台版 doc2md，跳过窗口版 doc2md-gui。")

# ---- 随程序一起带走的非代码文件 ----
datas = [(str(ROOT / "config.example.json"), ".")]
for extra in (".env.example", "README.md"):
    p = ROOT / extra
    if p.exists():
        datas.append((str(p), "."))

# ---- 版面模型（49MB，必须带）------------------------------------------------
# `pymupdf-layout` 是个**独立的发行包**，它不经 pip 的常规路径落文件，而是直接
# 把版面模型塞进 `pymupdf/layout/` 目录里。PyInstaller 只按 `pymupdf` 自己的
# 清单收集二进制，看不见这些"外来的"数据文件 —— 打出来的包看着正常，一跑
# `import pymupdf4llm` 就炸：
#
#     pymupdf4llm/__init__.py:52  →  pymupdf.layout.activate()
#       →  BoxRFDGNN.__init__  open('.../resources/onnx/layout_rf2.4.1+imf1.yaml')
#       →  FileNotFoundError
#
# 后果是**所有带文字层的 PDF 全部转不出来**。实测全量语料 3809 个文件里
# pdf_text 那一类 292 个全灭（另有 27 个报的是 AttributeError —— 半初始化的
# 模块被留在 sys.modules 里，同一个根因的另一种表现）。
#
# 不要试图只挑几个 .onnx「够用就行」：BoxRFDGNN 的表格网格模型有 10 个版本
# （V1/V1A/V1T/V2/V2A/V2B/V3/V4/V1T-A/V1T-B），feature_set 也有 3 种组合，
# 少一个就是某一类版面在运行期静默失败 —— 正是这次要修掉的故障形态。
datas += collect_data_files("pymupdf", subdir="layout")

# ---- 静态分析容易漏掉的导入 ----
hiddenimports = [
    # 核心第三方库显式列出，避免被误判为未使用
    "pymupdf", "pymupdf4llm", "mammoth", "markdownify",
    "openpyxl", "xlrd", "requests", "bs4",
]
if sys.platform == "win32":
    hiddenimports += [
        # pywin32：.doc/.xls 的 COM 转换，是在函数内部 import 的
        "win32com", "win32com.client", "win32com.client.dynamic",
        "pythoncom", "pywintypes", "win32timezone",
        # watchdog：Windows 下按平台动态挑 observer 实现
        "watchdog.observers.winapi",
        "watchdog.observers.read_directory_changes",
    ]
else:
    # 非 Windows：老式 .doc/.xls 走 LibreOffice soffice（不再 import pywin32），
    # watchdog 在 POSIX 下用 inotify / kqueue / polling observer。
    hiddenimports += [
        "watchdog.observers.inotify",
        "watchdog.observers.polling",
    ]
# 本包自己的模块：gui.py 里是「用到才 import」的（为了让窗口秒开），
# 静态分析对函数体内的 import 不够稳，直接整包收进来最省心。
hiddenimports += collect_submodules("doc2md")
hiddenimports += ["launcher"]
if HAS_TK:
    hiddenimports += ["gui"]

# ---- vendor 子项目：ZhDocParser（纯规则 PDF 结构还原）------------------------
# 两件必须做的事，少一件打出来的包就跑不了 rule 档：
#   1. pathex 指到 vendor/ZhDocParser —— 它不在默认搜索路径上，源码态靠
#      doc2md/pdf_zhdoc.py 里运行时插 sys.path，打包态必须由这里告诉分析器；
#   2. 显式列出要用的模块 —— 接入层是在**函数体内** import 的，静态分析对
#      函数体内的 import 不够稳（同 gui/launcher 的处理）。
# 只列 PDF 链路真正需要的：不列 zhdocparser.api / app / cli，它们的 fastapi /
# uvicorn / typer 就不进包；不列 extractors.factory，python-docx / lxml 也不进包。
VENDOR_ZH = ROOT / "vendor" / "ZhDocParser"
pathex = [str(ROOT)]
if VENDOR_ZH.is_dir():
    pathex.append(str(VENDOR_ZH))
    hiddenimports += [
        "zhdocparser",
        "zhdocparser.schemas",
        "zhdocparser.schemas.document",
        "zhdocparser.extractors",
        "zhdocparser.extractors.base",
        "zhdocparser.extractors.pdf_extractor",
    ]


# ---- 确定用不到的，排掉以减小体积 ----
# 注意：**不要**排 tkinter —— 窗口版要用它。
excludes = [
    "matplotlib", "pytest", "IPython",
    "pandas", "scipy", "notebook", "docutils",
    # PIL 是本机为「截图看界面」临时装进 .venv-gui 的，程序本身一行都没用
    # （requirements.txt 里也没有 pillow）。不排掉的话会白白带 11MB。
    "PIL",
]

a = Analysis(
    ["app.py"],
    pathex=pathex,
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

pyz = PYZ(a.pure)                        # noqa: F821

# ---- 打包形态：onedir（默认）/ onefile（单文件）------------------------------
# onedir：exe + _internal/，启动最快，与 Windows 版一致。
# onefile：依赖与内置资源全部打进单个可执行文件，便于整包分发；代价是每次
#   启动都要把内容解包到临时目录（_MEIPASS，退出即删），启动慢若干秒、占 /tmp。
#   为免体积翻倍，单文件模式只出控制台版。
ONEFILE = os.environ.get("DOC2MD_ONEFILE", "").strip().lower() not in ("", "0", "false", "no")

if ONEFILE:
    # ---- 单文件：只出一个控制台可执行文件 ----
    # 程序目录仍按 sys.executable 解析（不是 _MEIPASS），所以 config.json /
    # .env / state.db 依旧落在可执行文件旁边，随文件搬走。
    exe = EXE(                           # noqa: F821
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="doc2md",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,                    # 菜单/日志都要能看到，必须带控制台
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
else:
    # ---- 控制台版：菜单 + 命令行 ----
    exe = EXE(                           # noqa: F821
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="doc2md",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,                       # 不依赖本机装 UPX
        console=True,                    # 菜单/日志都要能看到，必须带控制台
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )

    # ---- 窗口版：双击直接开图形界面（解释器没有 tkinter 时跳过）--------------
    if HAS_TK:
        exe_gui = EXE(                       # noqa: F821
            pyz,
            a.scripts,
            [],
            exclude_binaries=True,
            name="doc2md-gui",
            debug=False,
            bootloader_ignore_signals=False,
            strip=False,
            upx=False,
            console=False,                   # 不要黑窗口
            disable_windowed_traceback=False,  # 崩了弹窗显示 traceback，好排查
            argv_emulation=False,
            target_arch=None,
            codesign_identity=None,
            entitlements_file=None,
        )
        collected = [exe, exe_gui]
    else:
        collected = [exe]

    coll = COLLECT(                      # noqa: F821
        *collected,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="doc2md",
    )
