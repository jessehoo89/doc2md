"""图形界面（Tkinter）—— 文档批量转 Markdown。

设计原则：**界面只做「收集参数 + 显示进度/日志」，不复制任何转换逻辑**。
窗口里按按钮做的事，就是 `python -m doc2md <子命令>` 做的事：

    扫描试运行 → Engine.run(dry_run=True)
    开始转换   → Engine.run(dry_run=False)
    启动监控   → WatchService.start(catch_up=True)
    查看统计   → StateStore.stats()
    重试失败   → StateStore.retry_paths() + Engine.process()
    检测 OCR   → build_router().ping_all()

因此 GUI 与命令行版的行为永远一致，不存在「两套实现各自 drift」的问题。

零新增依赖：tkinter 是 Python 标准库。打包后是 dist\\doc2md\\doc2md-gui.exe
（与控制台版 doc2md.exe 共享同一个 _internal 目录，只多占几 MB）。

线程模型（Tkinter 不是线程安全的，这一点必须做对）：
  · 所有耗时操作都在 worker 线程里跑，主线程只跑 mainloop；
  · worker 通过 queue 回传日志 / 进度，主线程用 after() 定时消费；
  · 「停止」置 threading.Event，Engine 通过 should_stop 钩子看到它，
    在当前文件转换完后不再取下一个任务。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# 与 launcher.py 一致：源码运行时保证能 import 到 doc2md 包；打包后这里是 exe 所在目录。
TOOL_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from doc2md.config import (          # noqa: E402  （config/state 都是轻量模块，可以顶上加载）
    CRED_FIELDS,
    Config,
    any_token_filled,
    bootstrap_config,
    describe_backends,
    describe_credentials,
    describe_env_file,
    describe_output,
    describe_tokens,
    dismiss_token_prompt,
    env_file_path,
    env_template_text,
    filled_aliases,
    load_config,
    mask_token,
    resolve_config_path,
    save_env_values,
    token_prompt_dismissed,
)
from doc2md.state import StateStore   # noqa: E402

APP_TITLE = "文档批量转 Markdown"
APP_VERSION = "1.0.0"
LOG_MAX_LINES = 5000                  # 日志面板上限，超出丢弃最旧的（长批量不至于吃满内存）

# 日志着色规则：按顺序匹配，先命中先用（"====" 一条必须排在最前面）
_COLOR_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("head", ("====",)),
    ("err", ("[错误]", "[失败]", "FAIL", "✗", "Traceback", "Error:")),
    ("ok", ("] OK", "✓", "转换完成", "重试完成")),
    ("warn", ("[警告]", "[注意]", "[提示]", "[熔断]", "[已停止]", "HOLD", "BLK ", "⊘")),
    ("info", ("[1/3]", "[2/3]", "[3/3]", "[配置]", "[监控]", "已监控", "监控中")),
)

# 凭据文件模板与 CLI / launcher 同源（config.env_template_text），
# 不再各留一份字面量 —— 以前两处模板不一致，改了一处另一处还是旧的。
class _NullIO:
    """窗口版没有控制台时，把第三方库的 print 吞掉。

    打包成 console=False 的 exe 后，sys.stdout / sys.stderr 可能是 None，
    而 local_ocr.py / com.py / ocr.py 里都有 `print(msg, flush=True)` 的兜底日志，
    直接打到 None 上会抛 AttributeError。挂个哑对象最省事。
    """

    encoding = "utf-8"

    def write(self, s):          # noqa: D102
        return len(s) if isinstance(s, str) else 0

    def flush(self):             # noqa: D102
        pass

    def isatty(self) -> bool:    # noqa: D102
        return False

    def fileno(self):            # noqa: D102
        raise OSError("no fileno")


def _guard_stdio() -> None:
    if sys.stdout is None:
        sys.stdout = _NullIO()   # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = _NullIO()   # type: ignore[assignment]


def _enable_dpi_awareness() -> None:
    """让窗口在高分屏上不发虚。必须在创建 Tk() 之前调用。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # per-monitor
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()        # 老系统的退路
    except Exception:
        pass


def _is_widgeted() -> bool:
    return True


class Doc2MdApp:
    POLL_MS = 80

    def __init__(self, root: tk.Tk, config_path: str | Path | None = None):
        self.root = root
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.stop_event = threading.Event()
        self.busy = False
        self._svc = None                  # 监控模式下的 WatchService，供「停止」调用
        self._worker: threading.Thread | None = None
        self.cfg: Config | None = None
        self.config_path: Path = resolve_config_path(config_path)
        self._cfg_widgets: list[tk.Widget] = []   # 运行中要锁住的配置控件
        self._action_widgets: list[ttk.Button] = []  # 运行中要禁用的启动按钮
        self._sash_done = False                   # 左栏宽度是否已经钉好（见 _place_sash）
        self._closing = False                     # 正在关闭：置位后所有 after 回调自行退出
        self._got_progress = False                # 本轮任务是否收到过具体进度（见 _drain_events）

        self._init_style()
        self._build_ui()
        self._load_initial_config()
        self._bind_shortcuts()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(self.POLL_MS, self._drain_events)
        self.log(f"{APP_TITLE} v{APP_VERSION}　图形界面")
        self.log(f"程序目录：{TOOL_DIR}")
        self.log(f"配置文件：{self.config_path}")
        self.log(f"云端 OCR Token：{describe_tokens()}")
        self.log("提示：日志面板的内容与命令行版完全一致，可直接对照排查。\n")

        # 装机后第一次打开（凭据文件还不存在）直接把填写窗口弹出来，见 _maybe_prompt_tokens
        self.root.after(600, self._maybe_prompt_tokens)

    # ================= 界面搭建 =================
    def _init_style(self) -> None:
        self.root.title(f"{APP_TITLE} v{APP_VERSION}")
        w, h = 1160, 780
        try:                                  # 开在屏幕中间偏上，比 Tk 默认的左上角顺手
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            self.root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        except Exception:
            self.root.geometry(f"{w}x{h}")
        self.root.minsize(980, 640)

        try:
            # DPI 缩放：tk scaling 按「每英寸像素 / 72」设，字号才会跟着系统缩放走
            self.root.tk.call("tk", "scaling", self.root.winfo_fpixels("1i") / 72.0)
        except Exception:
            pass

        style = ttk.Style()
        try:
            if "vista" in style.theme_names():
                style.theme_use("vista")
        except Exception:
            pass
        style.configure("Tool.TButton", padding=(9, 5))
        style.configure("Go.TButton", padding=(11, 5))
        style.configure("Hint.TLabel", foreground="#5a6270")
        style.configure("Group.TLabelframe.Label", foreground="#1f4e79")

        import tkinter.font as tkfont

        families = set(tkfont.families())
        for fam in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC"):
            if fam in families:
                for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                             "TkTooltipFont", "TkSmallCaptionFont"):
                    try:
                        tkfont.nametofont(name).configure(family=fam)
                    except Exception:
                        pass
                break
        # 等宽字体单独建一个并持有引用 —— 否则会被 GC 掉，日志区会悄悄退回默认字体
        mono = "Consolas" if "Consolas" in families else "Courier New"
        self.mono_font = tkfont.Font(family=mono, size=9)

    def _build_ui(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        self._build_menu()
        self._build_toolbar()          # row 0
        self._build_body()             # row 1
        self._build_statusbar()        # row 2

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="保存配置", command=self._on_save, accelerator="Ctrl+S")
        m_file.add_separator()
        m_file.add_command(label="填写云端 OCR Token…", command=self._on_tokens)
        m_file.add_command(label="打开配置文件", command=self._open_config)
        m_file.add_command(label="打开凭据文件 (.env)", command=self._open_env)
        m_file.add_command(label="打开程序目录", command=lambda: self._open(TOOL_DIR))
        m_file.add_separator()
        m_file.add_command(label="退出", command=self._on_close)
        menubar.add_cascade(label="文件", menu=m_file)

        m_log = tk.Menu(menubar, tearoff=0)
        m_log.add_command(label="清空日志", command=self._clear_log)
        m_log.add_command(label="日志另存为…", command=self._save_log)
        menubar.add_cascade(label="日志", menu=m_log)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="关于", command=self._on_about)
        menubar.add_cascade(label="帮助", menu=m_help)
        self.root.configure(menu=menubar)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        bar.grid(row=0, column=0, sticky="ew")

        def btn(text, cmd, *, primary=False, tip=""):
            b = ttk.Button(bar, text=text, command=cmd,
                           style=("Go.TButton" if primary else "Tool.TButton"))
            b.pack(side="left", padx=(0, 6))
            self._action_widgets.append(b)
            return b

        btn("扫描试运行", self.on_scan, tip="只看看有多少文件、走哪条通道，不写任何文件")
        btn("开始转换", self.on_run, primary=True, tip="全量转换，中断后重跑自动续传")
        btn("启动监控", self.on_watch, tip="常驻监控新增/修改的文件")
        self.btn_stop = ttk.Button(bar, text="停止", command=self._on_stop,
                                   style="Tool.TButton", state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 12))

        btn("转换单个文件…", self.on_test)
        btn("查看统计", self.on_status)
        btn("重试失败", self.on_retry)
        btn("检测云端 OCR", self.on_ping)

    def _build_body(self) -> None:
        pw = ttk.PanedWindow(self.root, orient="horizontal")
        pw.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 4))
        self.paned = pw

        # 顺序很关键：**先把内容建好，再 add 进 PanedWindow**。
        # 反过来（先 add 空 Frame、之后再塞控件）时，PanedWindow 按「加入那一刻」
        # 的 reqwidth 定分栏，而那会儿 Frame 还是空的 → 左栏被压成 1px 宽（实测踩到过，
        # 界面上表现为左侧配置面板整个消失、日志区铺满全窗口）。
        left = ttk.Frame(pw)
        self._build_scroll_area(left)

        right = ttk.Frame(pw)
        self._build_log_panel(right)

        pw.add(left, weight=0)
        pw.add(right, weight=1)

        # 分栏位置：<Map>（窗口真正显示）+ 几次延迟兜底。见 _place_sash 的说明。
        self.root.bind("<Map>", self._place_sash, add="+")
        for delay in (60, 220, 600):
            self.root.after(delay, self._place_sash)

    def _place_sash(self, *_a) -> None:
        """把左栏宽度钉在 404px。

        为什么不能靠 PanedWindow 自己分：在 Windows 上它的初始 sash 位置实测是 0，
        pane0 被压成 1px（左侧配置面板整个看不见，日志区铺满窗口）。而 sashpos()
        只有在窗口完成布局后设置才生效 —— 所以这里用 <Map> + 多次延迟重试。
        设置成功一次就置标志位收工，之后用户手动拖动分栏不会被覆盖。
        """
        if self._sash_done or self._closing:
            return
        try:
            if self.paned.winfo_width() > 200:      # 还没布局完就设，会被忽略
                self.paned.sashpos(0, 404)
                self._sash_done = True
        except Exception:
            pass

    def _build_scroll_area(self, parent: ttk.Frame) -> None:
        """把左侧配置面板放进可滚动容器。

        配置项（目录 / 输出 / 选项 / 摘要 / 一排按钮）加起来比 760px 高 ——
        小窗口、笔记本屏或 150% 缩放下底部会被窗口边缘切掉，而且没法滚。
        这里用 Canvas 套一层，内容超了就能滚。
        """
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        try:
            bg = ttk.Style().lookup("TFrame", "background") or "#f0f0f0"
        except Exception:
            bg = "#f0f0f0"

        canvas = tk.Canvas(parent, width=382, highlightthickness=0, bd=0, background=bg)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=vsb.set)
        self._cfg_canvas = canvas

        inner = ttk.Frame(canvas)
        self._cfg_inner = inner
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win, width=e.width))

        def _wheel(e):
            # 内容没超高时不滚，避免用户以为界面卡住
            if canvas.bbox("all") and canvas.bbox("all")[3] > canvas.winfo_height():
                canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

        # 只在指针进入配置区时接管滚轮：否则会把日志区的滚动一起抢走
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        self._build_config_panel(inner)

    # ---- 左：配置 ----
    def _build_config_panel(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        row = 0

        # 处理目录
        g1 = ttk.LabelFrame(parent, text=" 处理目录 ", padding=8)
        g1.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        g1.columnconfigure(0, weight=1)
        row += 1

        box = ttk.Frame(g1)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)
        # width=1 不是笔误：Listbox 默认按 20 个字符算请求宽度，撑得整个配置面板
        # 比可视宽度还宽（右边缘被切）。给个最小宽度，实际宽度交给 sticky="ew" 拉伸。
        self.lb_roots = tk.Listbox(box, height=6, width=1, selectmode=tk.EXTENDED,
                                   activestyle="none", exportselection=False)
        self.lb_roots.grid(row=0, column=0, sticky="ew")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.lb_roots.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.lb_roots.configure(yscrollcommand=sb.set)

        ops = ttk.Frame(g1)
        ops.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ops.columnconfigure(0, weight=1)
        ops.columnconfigure(1, weight=1)
        # 排成 2×2 而不是排成一行：4 个按钮横排的请求宽度实测是 436px，
        # 比配置面板还宽 → 右边缘会被窗口切掉。2×2 后只要 214px。
        b_add = self._mk_cfg_btn(ops, "添加目录…", self._on_add_root)
        b_del = self._mk_cfg_btn(ops, "移除选中", self._on_del_root)
        b_up = self._mk_cfg_btn(ops, "上移", lambda: self._move_root(-1))
        b_dn = self._mk_cfg_btn(ops, "下移", lambda: self._move_root(1))
        b_add.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 4))
        b_del.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 4))
        b_up.grid(row=1, column=0, sticky="ew", padx=(0, 4))
        b_dn.grid(row=1, column=1, sticky="ew", padx=(4, 0))

        # 输出方式
        g2 = ttk.LabelFrame(parent, text=" Markdown 输出 ", padding=8)
        g2.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        g2.columnconfigure(1, weight=1)
        row += 1

        self.var_out_mode = tk.StringVar(value="alongside")
        r1 = ttk.Radiobutton(g2, text="与原文件同目录、同名（推荐）", value="alongside",
                             variable=self.var_out_mode, command=self._sync_out_state)
        r1.grid(row=0, column=0, columnspan=3, sticky="w")
        r2 = ttk.Radiobutton(g2, text="统一存到指定目录：", value="custom",
                             variable=self.var_out_mode, command=self._sync_out_state)
        r2.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.var_out_root = tk.StringVar()
        self.ent_out_root = ttk.Entry(g2, textvariable=self.var_out_root)
        self.ent_out_root.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(18, 4))
        self.btn_out_browse = ttk.Button(g2, text="浏览…", width=8,
                                         command=self._on_pick_out_root)
        self.btn_out_browse.grid(row=2, column=2, sticky="e")
        self.lbl_out_hint = ttk.Label(g2, text="", style="Hint.TLabel", wraplength=340,
                                      justify="left")
        self.lbl_out_hint.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # 开关
        g3 = ttk.LabelFrame(parent, text=" 选项 ", padding=8)
        g3.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        row += 1
        self.var_keep = tk.BooleanVar(value=True)
        self.var_ocr = tk.BooleanVar(value=True)
        self.var_local = tk.BooleanVar(value=True)
        for i, (text, var, hint) in enumerate((
            ("保留原文件", self.var_keep, "转换后不删除/不改动源文件"),
            ("启用云端 OCR", self.var_ocr, "扫描件走 PaddleOCR / MinerU 等云端链路"),
            ("启用本地 OCR", self.var_local, "需要 config.json 里的 local_ocr.python_exe"),
        )):
            cb = ttk.Checkbutton(g3, text=text, variable=var)
            cb.grid(row=i * 2, column=0, sticky="w")
            self._cfg_widgets.append(cb)
            ttk.Label(g3, text="    " + hint, style="Hint.TLabel").grid(
                row=i * 2 + 1, column=0, sticky="w", pady=(0, 4))

        # 配置摘要
        g4 = ttk.LabelFrame(parent, text=" 当前生效配置 ", padding=8)
        g4.grid(row=row, column=0, sticky="ew")
        g4.columnconfigure(0, weight=1)
        row += 1
        self.lbl_chain = ttk.Label(g4, text="后端链路：—", wraplength=340, justify="left")
        self.lbl_chain.grid(row=0, column=0, sticky="w")
        self.lbl_cred = ttk.Label(g4, text="凭据文件：—", wraplength=340, justify="left",
                                  style="Hint.TLabel")
        self.lbl_cred.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.lbl_state = ttk.Label(g4, text="状态库：—", wraplength=340, justify="left",
                                   style="Hint.TLabel")
        self.lbl_state.grid(row=2, column=0, sticky="w", pady=(4, 0))

        # 凭据入口：装完机第一件要干的事就是填 Token，所以单独给一行显眼位置。
        # 注意**不放进 _cfg_widgets**：转换过程中也允许补填 Token（下一轮生效）。
        g4b = ttk.Frame(parent)
        g4b.grid(row=row, column=0, sticky="ew", pady=(8, 0))
        row += 1
        ttk.Button(g4b, text="填写云端 OCR Token…", style="Go.TButton",
                   command=self._on_tokens).pack(side="left")
        ttk.Button(g4b, text="检测云端 OCR", style="Tool.TButton",
                   command=self.on_ping).pack(side="left", padx=6)

        # 打开类动作
        g5 = ttk.Frame(parent)
        g5.grid(row=row, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(g5, text="保存配置", style="Tool.TButton",
                   command=self._on_save).pack(side="left")
        ttk.Button(g5, text="打开配置文件", style="Tool.TButton",
                   command=self._open_config).pack(side="left", padx=6)
        ttk.Button(g5, text="打开凭据文件", style="Tool.TButton",
                   command=self._open_env).pack(side="left")
        g6 = ttk.Frame(parent)
        g6.grid(row=row + 1, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(g6, text="打开程序目录", style="Tool.TButton",
                   command=lambda: self._open(TOOL_DIR)).pack(side="left")
        ttk.Button(g6, text="重新加载配置", style="Tool.TButton",
                   command=self._on_reload_click).pack(side="left", padx=6)

    def _mk_cfg_btn(self, parent, text, cmd) -> ttk.Button:
        b = ttk.Button(parent, text=text, style="Tool.TButton", command=cmd)
        self._cfg_widgets.append(b)
        return b

    # ---- 右：日志 ----
    def _build_log_panel(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        head = ttk.Frame(parent)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(head, text="运行日志").pack(side="left")
        ttk.Button(head, text="清空", style="Tool.TButton",
                   command=self._clear_log).pack(side="right")
        ttk.Button(head, text="另存为…", style="Tool.TButton",
                   command=self._save_log).pack(side="right", padx=6)

        wrap = ttk.Frame(parent)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)

        self.log_text = tk.Text(wrap, wrap="none", undo=False, height=10,
                                font=self.mono_font, background="#ffffff",
                                foreground="#1f2328", insertbackground="#1f2328",
                                relief="solid", borderwidth=1, padx=6, pady=4)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=self.log_text.yview)
        ysb.grid(row=0, column=1, sticky="ns")
        xsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.log_text.xview)
        xsb.grid(row=1, column=0, sticky="ew")
        self.log_text.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)

        for tag, color in (("head", "#1f4e79"), ("err", "#b00020"), ("warn", "#96560a"),
                           ("ok", "#0b6b3a"), ("info", "#3a4a63")):
            self.log_text.tag_configure(tag, foreground=color)
        self.log_text.tag_configure("head", font=(self.mono_font.actual("family"), 9, "bold"))
        self.log_text.configure(state="disabled")

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(10, 2, 10, 8))
        bar.grid(row=2, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)

        self.prog = ttk.Progressbar(bar, mode="determinate", length=260)
        self.prog.grid(row=0, column=0, sticky="w")
        self.lbl_status = ttk.Label(bar, text="就绪")
        self.lbl_status.grid(row=0, column=1, sticky="w", padx=10)
        self.lbl_counter = ttk.Label(bar, text="", style="Hint.TLabel")
        self.lbl_counter.grid(row=0, column=2, sticky="e")

    def _bind_shortcuts(self) -> None:
        self.root.bind("<F5>", lambda e: self.on_scan())
        self.root.bind("<Control-s>", lambda e: self._on_save())
        self.root.bind("<Escape>", lambda e: self._on_stop() if self.busy else None)

    # ================= 配置读写 =================
    def _load_initial_config(self) -> None:
        if not self.config_path.exists():
            bootstrap_config(self.config_path)
        self._reload_config()
        self._sync_form_from_config()

    def _reload_config(self) -> None:
        try:
            self.cfg = load_config(self.config_path)
        except Exception as e:
            self.cfg = None
            self.lbl_chain.configure(text="后端链路：—")
            self.lbl_cred.configure(text="凭据文件：—")
            self.lbl_state.configure(text="状态库：—")
            messagebox.showerror(APP_TITLE, f"配置加载失败：\n{self.config_path}\n\n{e}")
            return
        cfg = self.cfg
        self.lbl_chain.configure(text="后端链路：\n  " + describe_backends(cfg))
        self.lbl_cred.configure(text="凭据文件：\n  " + describe_env_file(cfg))
        self.lbl_state.configure(text=f"状态库：{cfg.state_db}\n日志目录：{cfg.log_dir}")

    def _raw(self) -> dict:
        return (self.cfg.raw if self.cfg is not None and isinstance(self.cfg.raw, dict) else {})

    def _sync_form_from_config(self) -> None:
        """把 config.json 的值填进界面。

        关键点：一律读 **原始 json**，不读 load_config 归一化后的对象 ——
        归一化会做「local_ocr 没配 python_exe 就自动禁用」「roots 为空就回退到程序目录」
        这类修正，拿它回填界面再写回去，就会把用户原本的意图悄悄改掉。
        """
        raw = self._raw()

        self.lb_roots.delete(0, "end")
        for r in (raw.get("roots") or []):
            self.lb_roots.insert("end", str(r))

        raw_out = raw.get("output") or {}
        mode = str(raw_out.get("mode") or (self.cfg.output.mode if self.cfg else "alongside"))
        self.var_out_mode.set("custom" if mode == "custom" else "alongside")
        self.var_out_root.set(str(raw_out.get("root") or ""))

        raw_ocr = raw.get("ocr") or {}
        raw_local = raw.get("local_ocr") or {}
        self.var_keep.set(bool(raw.get("keep_original", True)))
        self.var_ocr.set(bool(raw_ocr.get("enabled", True)))
        self.var_local.set(bool(raw_local.get("enabled", False)))
        self._sync_out_state()

    def _sync_out_state(self) -> None:
        custom = self.var_out_mode.get() == "custom"
        state = "normal" if custom else "disabled"
        self.ent_out_root.configure(state=state)
        self.btn_out_browse.configure(state=state)
        if custom and not self.var_out_root.get().strip():
            self.lbl_out_hint.configure(text="⚠ 目录为空时程序会自动退回「与原文件同目录」。")
        elif custom:
            self.lbl_out_hint.configure(
                text="目录结构按 config.json 的 output.layout 决定（mirror 保留原目录结构）。")
        else:
            self.lbl_out_hint.configure(text="")

    def _apply_form(self) -> bool:
        """把界面上的配置写回 config.json（原子替换），然后重新加载。

        每个动作执行前都会先调它 —— 这样「界面所见」永远就是「实际所用」，
        不需要用户记得先去点保存。
        """
        path = self.config_path
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"配置文件读取失败：\n{path}\n\n{e}")
            return False
        if not isinstance(data, dict):
            data = {}

        data["roots"] = [str(r) for r in self.lb_roots.get(0, "end")]

        out = dict(data.get("output") or {})
        out["mode"] = "custom" if self.var_out_mode.get() == "custom" else "alongside"
        out["root"] = self.var_out_root.get().strip()
        out.setdefault("layout", "mirror")
        out.setdefault("on_collision", "stable")
        data["output"] = out

        data["keep_original"] = bool(self.var_keep.get())

        ocr = dict(data.get("ocr") or {})
        ocr["enabled"] = bool(self.var_ocr.get())
        data["ocr"] = ocr

        local = dict(data.get("local_ocr") or {})
        local["enabled"] = bool(self.var_local.get())
        data["local_ocr"] = local

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)          # 原子替换：中途崩了也不会留下半个配置文件
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"配置文件写入失败：\n{path}\n\n{e}")
            return False

        self._reload_config()
        if out["mode"] == "custom" and not out["root"]:
            self.log("[提示] 选了「存到指定目录」但目录为空 → 本次按「与原文件同目录」处理。")
        return True

    def _on_save(self) -> None:
        if self._apply_form():
            self.log("[配置] 已保存到 " + str(self.config_path))

    def _on_reload_click(self) -> None:
        self._reload_config()
        self._sync_form_from_config()
        self.log("[配置] 已从磁盘重新加载。")

    # ---- 目录列表操作 ----
    def _on_add_root(self) -> None:
        d = filedialog.askdirectory(title="选择要扫描的目录", mustexist=True)
        if not d:
            return
        d = str(Path(d))
        existing = [str(x) for x in self.lb_roots.get(0, "end")]
        if d in existing:
            messagebox.showinfo(APP_TITLE, "该目录已在列表里。")
            return
        self.lb_roots.insert("end", d)

    def _on_del_root(self) -> None:
        sel = list(self.lb_roots.curselection())
        if not sel:
            messagebox.showinfo(APP_TITLE, "请先在列表里选中要移除的目录。")
            return
        for i in reversed(sel):
            self.lb_roots.delete(i)

    def _move_root(self, delta: int) -> None:
        sel = list(self.lb_roots.curselection())
        if len(sel) != 1:
            return
        i = sel[0]
        j = i + delta
        if not (0 <= j < self.lb_roots.size()):
            return
        val = self.lb_roots.get(i)
        self.lb_roots.delete(i)
        self.lb_roots.insert(j, val)
        self.lb_roots.selection_clear(0, "end")
        self.lb_roots.selection_set(j)

    def _on_pick_out_root(self) -> None:
        d = filedialog.askdirectory(title="选择 Markdown 输出目录", mustexist=False)
        if d:
            self.var_out_root.set(str(Path(d)))

    # ---- 打开文件/目录 ----
    def _open(self, p: Path) -> None:
        try:
            os.startfile(str(p))            # type: ignore[attr-defined]
            return
        except Exception:
            pass
        try:
            if os.name == "nt":
                subprocess.Popen(["cmd", "/c", "start", "", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"无法打开：{p}\n{e}")

    def _open_config(self) -> None:
        if not self.config_path.exists():
            bootstrap_config(self.config_path)
        if not self.config_path.exists():
            messagebox.showerror(APP_TITLE, f"配置文件不存在，且无法从示例生成：{self.config_path}")
            return
        self._open(self.config_path)

    def _open_env(self) -> None:
        """打开凭据文件 .env；不存在就先落一份模板，别让用户对着空目录发愁。"""
        p = env_file_path(self.config_path)
        if not p.exists():
            try:
                p.write_text(env_template_text(), encoding="utf-8")
                self.log(f"[提示] 已生成凭据文件模板：{p}\n"
                         f"        把各平台的 Token 填在等号右侧即可；"
                         f"也可以直接用「填写云端 OCR Token…」界面来填。")
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"无法创建 {p}\n{e}")
                return
        self._open(p)

    # ---- 填写云端 OCR Token ----
    def _maybe_prompt_tokens(self) -> None:
        """没填过 Token 时，启动后直接把「填写 Token」窗口弹出来。

        判定条件是「常用 Token 一个都没填」，**不是"文件不存在"** ——
        装机包会把 `.env.example` 复制成一份空的 `.env`，按文件存在与否判断就
        永远不会弹，用户只能自己猜到哪里填。

        说过「稍后再说」就不再打扰（记号写在 .env 的注释行里，不额外造文件）。
        """
        if self._closing or os.environ.get("DOC2MD_NO_TOKEN_PROMPT"):
            return
        if any_token_filled():
            return
        env_path = env_file_path(self.config_path)
        if token_prompt_dismissed(env_path):
            return
        self.log("[提示] 还没填过云端 OCR 的 Token，已打开设置窗口。\n"
                 "        不填也能转：docx/xlsx 和带文字层的 PDF 不依赖 OCR；"
                 "扫描件则会退化成只走 MinerU 免鉴权接口（不出插图、精度较低）。")
        self._on_tokens(first_run=True)

    def _on_tokens(self, *, first_run: bool = False) -> None:
        """「填写云端 OCR Token」窗口：读现状 → 改 → 写回 .env（立即生效）。

        键名、说明、模板一律取自 config.CRED_FIELDS 这唯一一份定义，
        界面不另立一套文案，避免"界面能填、命令行不认"的漂移。
        """
        env_path = env_file_path(self.config_path)
        came_from = set((self.cfg.env_keys if self.cfg is not None else []) or [])

        win = tk.Toplevel(self.root)
        win.title("填写云端 OCR Token")
        win.transient(self.root)
        win.resizable(False, False)

        outer = ttk.Frame(win, padding=14)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)

        head = ("这些 Token 决定扫描件能不能走云端 OCR。填完保存立即生效，不用重启程序。\n"
                "留空＝不启用对应后端，不影响其它后端。Token 只写进本机凭据文件，"
                "不会进版本库，也不会出现在日志里。")
        ttk.Label(outer, text=head, style="Hint.TLabel", wraplength=560,
                  justify="left").grid(row=0, column=0, sticky="w", pady=(0, 10))

        rows: list[tuple[str, tk.StringVar]] = []

        def add_row(parent: ttk.Frame, f, r: int) -> int:
            """一个字段占两行：上行「名称 + 输入框 + 显示」，下行灰色说明。

            不要排成「名称一行、输入框一行、说明一行」——三个字段就多出 3 行，
            整个对话框会高到 690px，在 768p 笔记本上直接顶出屏幕。
            """
            secret = "TOKEN" in f.key or f.key.endswith("_KEY")
            ttk.Label(parent, text=f.label).grid(row=r, column=0, sticky="w", padx=(0, 6))
            var = tk.StringVar(value=(os.environ.get(f.key) or "").strip())
            ent = ttk.Entry(parent, textvariable=var, width=42)
            if secret:
                ent.configure(show="*")
            ent.grid(row=r, column=1, sticky="ew", padx=(0, 6))
            if secret:
                eye = tk.BooleanVar(value=False)
                ttk.Checkbutton(parent, text="显示", variable=eye,
                                command=lambda v=eye, e=ent:
                                e.configure(show="" if v.get() else "*")
                                ).grid(row=r, column=2, sticky="e")
            note = f.hint
            if f.where:
                note += f"　申请：{f.where}"
            if var.get() and f.key not in came_from:
                note = "⚠ 当前值来自「系统环境变量」，它的优先级高于本文件，" \
                       "在这里改不会生效（要改请改环境变量，或先把它删掉）。　" + note
            ttk.Label(parent, text=" " + note, style="Hint.TLabel", wraplength=540,
                      justify="left").grid(row=r + 1, column=0, columnspan=3,
                                           sticky="w", pady=(0, 6))
            rows.append((f.key, var))
            return r + 2

        g_common = ttk.LabelFrame(outer, text=" 常用（填了就能用云端 OCR） ", padding=10)
        g_common.grid(row=1, column=0, sticky="ew")
        g_common.columnconfigure(1, weight=1)
        r = 0
        for f in CRED_FIELDS:
            if not f.advanced:
                r = add_row(g_common, f, r)

        g_adv = ttk.LabelFrame(outer, text=" 高级（一般不用改） ", padding=10)
        g_adv.columnconfigure(1, weight=1)
        show_adv = tk.BooleanVar(value=False)

        def toggle_adv() -> None:
            if show_adv.get():
                g_adv.grid(row=2, column=0, sticky="ew", pady=(8, 0))
            else:
                g_adv.grid_remove()
            win.update_idletasks()

        ra = 0
        for f in CRED_FIELDS:
            if f.advanced:
                ra = add_row(g_adv, f, ra)
        ttk.Checkbutton(outer, text="显示高级选项（自建服务地址、通用 VLM 兜底凭证）",
                        variable=show_adv, command=toggle_adv
                        ).grid(row=3, column=0, sticky="w", pady=(8, 0))
        g_adv.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        g_adv.grid_remove()

        # 现有状态：凭据文件路径 + 各后端实际读到的凭据（脱敏）
        info = ttk.LabelFrame(outer, text=" 当前状态 ", padding=10)
        info.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        info.columnconfigure(0, weight=1)
        lines = [f"凭据文件：{env_path}",
                 f"填写情况：{describe_tokens()}"]
        aliases = filled_aliases()
        if aliases:
            lines.append("注意：检测到别名 " + "、".join(aliases) + " 也有值。"
                         "它的优先级与上面的规范名不一定相同，两个都填时请以「各后端实际凭据」为准。")
        if self.cfg is not None:
            lines.append("各后端实际凭据：")
            lines += [f"    {ln}" for ln in describe_credentials(self.cfg)]
        else:
            lines.append("（配置未加载成功，无法列出后端）")
        ttk.Label(info, text="\n".join(lines), style="Hint.TLabel", wraplength=560,
                  justify="left").grid(row=0, column=0, sticky="w")

        def collect() -> dict[str, str | None]:
            updates: dict[str, str | None] = {}
            for key, var in rows:
                new = var.get().strip()
                old = (os.environ.get(key) or "").strip()
                # 没改过、且值本来来自系统环境变量（不在文件里）→ 不动文件，
                # 免得把系统环境变量里的 Token 顺手抄进本机文件里
                if new == old and (key not in came_from or new):
                    continue
                updates[key] = new
            return updates

        def save(*, then_ping: bool) -> None:
            updates = collect()
            if not updates:
                self.log("[凭据] 没有需要保存的改动。")
                win.destroy()
                if then_ping:
                    self.on_ping()
                return
            try:
                save_env_values(updates, env_path)
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"写入凭据文件失败：\n{env_path}\n\n{e}",
                                     parent=win)
                return
            changed = [k for k, v in updates.items() if (v or "").strip()]
            cleared = [k for k, v in updates.items() if not (v or "").strip()]
            msg = f"[凭据] 已写入 {env_path}"
            if changed:
                msg += "\n        写入：" + "、".join(
                    f"{k}={mask_token(os.environ.get(k, ''))}" for k in changed)
            if cleared:
                msg += "\n        置空：" + "、".join(cleared)
            msg += "\n        已即时生效（无需重启）；当前 " + describe_tokens()
            self.log(msg)
            win.destroy()
            self._reload_config()
            if then_ping:
                self.on_ping()

        def cancel() -> None:
            # 首次启动时点了「稍后再说」就落下记号，别每次开程序都弹一遍。
            # 从菜单主动打开的情况（first_run=False）不记 —— 用户可能只是先看看。
            if first_run:
                try:
                    dismiss_token_prompt(env_path)
                except Exception:
                    pass
            win.destroy()

        btns = ttk.Frame(outer)
        btns.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(btns, text="保存", style="Go.TButton",
                   command=lambda: save(then_ping=False)).pack(side="left")
        ttk.Button(btns, text="保存并检测连通性",
                   command=lambda: save(then_ping=True)).pack(side="left", padx=6)
        ttk.Button(btns, text="打开凭据文件", command=self._open_env).pack(side="left")
        ttk.Button(btns, text="稍后再说", command=cancel).pack(side="right")

        win.bind("<Escape>", lambda e: cancel())
        win.bind("<Return>", lambda e: save(then_ping=False))
        win.protocol("WM_DELETE_WINDOW", cancel)

        if first_run:
            note = ("\n提示：一个 Token 都不填也能用 —— docx / xlsx / 有文字层的 PDF "
                    "本地就能转；\n只有扫描件（图片型 PDF、无文字层）才需要云端 OCR。")
            self.log(note.strip())

        # Windows 上给「刚建好、还没映射」的窗口设位置会被丢掉 —— 实测停在屏幕
        # 左上角 (0,0)，算出来的偏移量完全没生效。所以等窗口真正出现（<Map>）之后
        # 再钉一次，另外补两次延迟兜底，覆盖"窗口管理器先摆、后收到请求"的顺序差异。
        # 钉成功就摘掉钩子：否则用户把窗口拖到一边后，任何一次重映射都会把它拽回中间。
        placed = {"ok": False}

        def place(*_a) -> None:
            if placed["ok"]:
                return
            self._center_on_parent(win)
            if win.winfo_x() > 1 or win.winfo_y() > 1:      # 位置确实生效了
                placed["ok"] = True

        place()
        win.bind("<Map>", place, add="+")
        for _delay in (30, 200):
            win.after(_delay, place)
        win.grab_set()
        self.root.wait_window(win)

    def _center_on_parent(self, win: tk.Toplevel) -> None:
        """把对话框摆到主窗口中间，并**钳在屏幕内**。

        两件容易踩的事：
          · 主窗口还没完成布局时 winfo_width() 只有 1，直接拿它算会得到负偏移，
            对话框被甩到屏幕左上角（实测过）；
          · 高分屏 / 小笔记本上，对话框可能比屏幕还高，必须往下钳，
            否则标题栏和底部按钮会跑到屏幕外，用户只能拖窗口。
        """
        try:
            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            pw, ph = self.root.winfo_width(), self.root.winfo_height()
            px, py = self.root.winfo_rootx(), self.root.winfo_rooty()
            if pw < 200 or ph < 200:              # 主窗口还没布局完 → 按屏幕居中
                px = py = 0
                pw, ph = sw, sh
            x = px + max(0, (pw - w) // 2)
            y = py + max(0, (ph - h) // 3)
            x = max(0, min(x, sw - w))
            y = max(0, min(y, sh - h - 48))       # 48 留给任务栏 / 标题栏
            if os.environ.get("DOC2MD_DEBUG_GEOM"):    # 排布局问题时设上它，能看见算式
                print(f"[geom] w={w} h={h} sw={sw} sh={sh} pw={pw} ph={ph} "
                      f"px={px} py={py} -> +{x}+{y}", flush=True)
            win.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def _on_about(self) -> None:
        messagebox.showinfo(
            f"关于 {APP_TITLE}",
            f"{APP_TITLE} v{APP_VERSION}　图形界面\n\n"
            f"与命令行版共用同一套转换核心，行为完全一致。\n\n"
            f"程序目录：{TOOL_DIR}\n"
            f"配置文件：{self.config_path}\n\n"
            f".doc / .xls 这类老式格式走本机 WPS/Office 的 COM，目标机必须装；\n"
            f"本地 RapidOCR 需要 config.json 里指定的独立解释器。",
        )

    # ================= 日志与进度 =================
    def log(self, msg: str) -> None:
        """线程安全：Engine / WatchService / OCR 路由都拿它当 logger。"""
        self.events.put(("log", str(msg)))

    def _append_log(self, text: str) -> None:
        t = self.log_text
        at_bottom = t.yview()[1] > 0.999
        t.configure(state="normal")
        for line in text.splitlines():
            t.insert("end", line + "\n", self._tag_for(line))
        total = int(t.index("end-1c").split(".")[0])
        if total > LOG_MAX_LINES:
            t.delete("1.0", f"{total - LOG_MAX_LINES}.0")
        t.configure(state="disabled")
        if at_bottom:
            t.see("end")

    @staticmethod
    def _tag_for(line: str) -> str:
        for tag, needles in _COLOR_RULES:
            if any(n in line for n in needles):
                return tag
        return ""

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _save_log(self) -> None:
        p = filedialog.asksaveasfilename(
            title="日志另存为", defaultextension=".txt",
            initialfile=time.strftime("doc2md-%Y%m%d-%H%M%S.log"),
            filetypes=[("文本文件", "*.txt"), ("全部文件", "*.*")],
        )
        if not p:
            return
        try:
            Path(p).write_text(self.log_text.get("1.0", "end"), encoding="utf-8")
            self.log(f"[日志] 已保存到 {p}")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"保存失败：{e}")

    def _drain_events(self) -> None:
        """主线程定时消费 worker 回传的事件（Tkinter 只能在主线程里动控件）。"""
        # destroy() 之后排队中的 after 仍会触发，那时控件已消失，Tk 会抛
        # `invalid command name "..._drain_events"`。窗口版 exe 没有控制台，
        # 这种错会直接弹 traceback 框 —— 所以关闭后必须立刻停掉这个循环。
        if self._closing:
            return
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self._append_log(ev[1])
                elif kind == "progress":
                    idx, total = ev[1], ev[2]
                    self._got_progress = True
                    self.prog.configure(mode="determinate", maximum=max(1, total), value=idx)
                    self.lbl_counter.configure(text=f"{idx} / {total}")
                elif kind == "status":
                    self.lbl_status.configure(text=ev[1])
                elif kind == "pulse":
                    if ev[1]:
                        self.prog.configure(mode="indeterminate")
                        self.prog.start(60)
                    else:
                        # 只有「整轮都没有具体进度的任务」（如检测云端 OCR）收尾才归零。
                        # 批量转换结束时不能归零：进度条停在满格才说明「跑完了」，
                        # 归零会让人以为没跑过（归零由新任务开始时的 _start_job 负责）。
                        # 这里用自己的标志位判断，不去问 ttk 的 mode —— 那个返回值不可靠。
                        self.prog.stop()
                        self.prog.configure(mode="determinate")
                        if not self._got_progress:
                            self.prog.configure(value=0)
                elif kind == "busy":
                    self._set_busy(bool(ev[1]))
        except queue.Empty:
            pass
        except tk.TclError:
            return                            # 窗口正在销毁
        if not self._closing:
            self.root.after(self.POLL_MS, self._drain_events)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        st = "disabled" if busy else "normal"
        for b in self._action_widgets:
            try:
                b.configure(state=st)
            except Exception:
                pass
        for w in self._cfg_widgets:
            try:
                w.configure(state=st)
            except Exception:
                pass
        if not busy:
            self._sync_out_state()     # Entry/浏览 的可用性由输出模式决定，别被解锁成错误状态
        else:
            self.ent_out_root.configure(state="disabled")
            self.btn_out_browse.configure(state="disabled")
        try:
            self.lb_roots.configure(state=st)
        except Exception:
            pass
        self.btn_stop.configure(state=("normal" if busy else "disabled"))
        if not busy:
            self.lbl_status.configure(text="就绪")

    # ================= 任务调度 =================
    def _start_job(self, status: str, fn) -> None:
        if self.busy:
            messagebox.showinfo(APP_TITLE, "已有任务在运行，请先点「停止」或等它结束。")
            return
        self.stop_event.clear()
        self.lbl_status.configure(text=status)
        self.lbl_counter.configure(text="")
        self._got_progress = False
        self.prog.configure(mode="determinate", maximum=100, value=0)
        self._set_busy(True)

        def runner() -> None:
            try:
                fn()
            except Exception as e:                      # 任务异常不能把界面搞崩
                self.log(f"[错误] {type(e).__name__}: {e}")
                self.log(traceback.format_exc(limit=6).strip())
            finally:
                self.events.put(("pulse", False))
                self.events.put(("busy", False))

        self._worker = threading.Thread(target=runner, daemon=True, name="gui-job")
        self._worker.start()

    def _on_stop(self) -> None:
        if not self.busy:
            return
        self.stop_event.set()
        svc = self._svc
        if svc is not None:
            try:
                svc.stop()
            except Exception as e:
                self.log(f"[警告] 停止监控时出错：{type(e).__name__}: {e}")
        self.log("[停止] 已发出停止信号：正在转换的文件会做完，剩余的不再开始。")
        self.lbl_status.configure(text="正在停止…")

    def _prepare(self) -> Config | None:
        """写入界面配置 → 重新加载 → 返回 Config。"""
        if not self._apply_form():
            return None
        if self.cfg is None:
            self._reload_config()
        return self.cfg

    @staticmethod
    def _new_engine(cfg: Config, store: StateStore, stop: threading.Event, log):
        """延迟到用时才 import —— doc2md.engine 会连带拉起 pymupdf，窗口要能秒开。"""
        from doc2md.engine import Engine

        return Engine(cfg, store, verbose=True, logger=log, should_stop=stop.is_set)

    def _banner(self, cfg: Config, mode: str) -> None:
        self.log("=" * 74)
        self.log(f"  文档批量转 Markdown  ·  {mode}")
        self.log("=" * 74)
        self.log(f"  处理目录 : {', '.join(cfg.roots)}")
        self.log(f"  转换格式 : {', '.join(cfg.watch_extensions)}")
        self.log(f"  保留原文件: {'是' if cfg.keep_original else '否'}")
        self.log(f"  md 输出到 : {describe_output(cfg)}")
        self.log(f"  云端 OCR : {'启用' if cfg.ocr.enabled else '禁用'}")
        self.log(f"  后端链路 : {describe_backends(cfg)}")
        self.log(f"  凭据文件 : {describe_env_file(cfg)}")
        if cfg.local_ocr is not None:
            lo = cfg.local_ocr
            self.log(f"  本地 OCR : {'启用' if lo.enabled else '禁用'}"
                     f"（{lo.device}，复杂表{'转云端' if lo.ocr_complex_fallback else '本地启发式'}）")
        self.log("=" * 74)
        self.log("")

    def _on_progress(self, idx: int, total: int) -> None:
        self.events.put(("progress", idx, total))

    # ---- 各动作 ----
    def on_scan(self) -> None:
        cfg = self._prepare()
        if cfg is None:
            return

        def job() -> None:
            self.log("[引擎] 正在加载转换引擎…")
            self.log("")
            self._banner(cfg, "试运行 / 扫描")
            store = StateStore(cfg.state_db)
            eng = self._new_engine(cfg, store, self.stop_event, self.log)
            try:
                rep = eng.run(dry_run=True)
            finally:
                try:
                    eng.close()
                finally:
                    store.close()
            if self.stop_event.is_set():
                self.log(f"\n已停止。本轮已处理 {rep.ok + rep.skipped + rep.failed} 个。")
            else:
                self.log("\n[完成] 试运行未写入任何文件。确认无误后点「开始转换」。")

        self._start_job("扫描 / 试运行中…", job)

    def on_run(self) -> None:
        cfg = self._prepare()
        if cfg is None:
            return

        def job() -> None:
            self.log("[引擎] 正在加载转换引擎…")
            self.log("")
            self._banner(cfg, "批量转换")
            store = StateStore(cfg.state_db)
            eng = self._new_engine(cfg, store, self.stop_event, self.log)
            t0 = time.time()
            try:
                rep = eng.run(dry_run=False, on_progress=self._on_progress)
            finally:
                try:
                    eng.close()
                finally:
                    store.close()
            self._report_summary(rep, time.time() - t0)

        self._start_job("正在批量转换…", job)

    def _report_summary(self, rep, elapsed: float) -> None:
        self.log("\n" + "=" * 74)
        self.log("  转换完成" + ("（已中止）" if rep.aborted else ""))
        self.log("=" * 74)
        self.log(f"  成功 {rep.ok}   跳过 {rep.skipped}   失败 {rep.failed}   拦截 {rep.blocked}")
        if rep.deferred:
            self.log(f"  暂缓待重试 {rep.deferred}")
        if rep.aborted:
            self.log(f"  中止未处理 {rep.aborted}（重跑会自动跳过已完成的）")
        if rep.ocr_pages:
            self.log(f"  云端 OCR：{rep.ocr_files} 个文件 / {rep.ocr_pages} 页")
        if rep.ocr_by_backend:
            dist = "，".join(f"{k}={v}" for k, v in rep.ocr_by_backend.most_common())
            self.log(f"  后端用量：{dist}")
        if rep.ocr_switched:
            self.log(f"  熔断切换：{rep.ocr_switched} 个文件由备用后端接手")
        if rep.ocr_missing_images:
            self.log(f"  [注意] {rep.ocr_missing_images} 个文件的插图未被后端返回（md 中留有空占位）")
        self.log(f"  耗时 {elapsed:.1f} 秒")
        if rep.ocr_paused:
            self.log("")
            self.log("  [注意] 云端 OCR 链路全部不可用，本轮已熔断剩余 OCR 任务。")
            self.log("         本地转换（Office / 文本 PDF）不受影响。")
            self.log("         稍后点「重试失败」即可续跑这些文件。")
        if rep.by_engine:
            self.log("  按引擎分布：" + "，".join(f"{k}={v}" for k, v in rep.by_engine.most_common()))
        if rep.errors:
            self.log("")
            self.log(f"  失败明细（前 15 条，共 {len(rep.errors)}）：")
            for p, e in rep.errors[:15]:
                self.log(f"    - {Path(p).name}")
                self.log(f"        {e}")
            self.log("")
            self.log("  点工具栏的「重试失败」可以只重跑这些文件。")
        self.log("=" * 74)

    def on_watch(self) -> None:
        cfg = self._prepare()
        if cfg is None:
            return

        def job() -> None:
            from doc2md.watcher import WatchService

            self.log("[引擎] 正在加载转换引擎…")
            self.log("")
            self._banner(cfg, "实时监控")
            store = StateStore(cfg.state_db)
            eng = self._new_engine(cfg, store, self.stop_event, self.log)
            svc = WatchService(cfg, store, eng, verbose=True, logger=self.log,
                               config_path=self.config_path)
            self._svc = svc
            try:
                svc.start(catch_up=True)
            finally:
                self._svc = None
                try:
                    store.close()
                except Exception:
                    pass
            self.log("[监控] 已退出。")

        self._start_job("监控运行中…", job)

    def on_status(self) -> None:
        cfg = self._prepare()
        if cfg is None:
            return

        def job() -> None:
            store = StateStore(cfg.state_db)
            try:
                s = store.stats()
            finally:
                store.close()
            self.log("=" * 74)
            self.log("  转换状态统计")
            self.log("=" * 74)
            self.log(f"  数据库   : {cfg.state_db}")
            self.log(f"  已记录   : {s['total']} 个文件")
            names = {"ok": "成功", "skipped": "跳过", "failed": "失败",
                     "deferred": "待重试", "blocked": "拦截"}
            for k, v in sorted(s["by_status"].items(), key=lambda x: -x[1]):
                self.log(f"    {names.get(k, k):<10} {v}")
            if s["by_engine"]:
                self.log("  按引擎：")
                for k, v in list(s["by_engine"].items())[:12]:
                    self.log(f"    {str(k):<12} {v}")
            self.log(f"  累计页数 : {s['pages_total']}（今日 {s['pages_today']}）")
            bu = s.get("backend_pages_today") or {}
            if bu:
                self.log("  今日后端用量：")
                for name, pages in bu.items():
                    lim = 0
                    for b in cfg.ocr_backends:
                        if b.name == name:
                            lim = b.daily_page_limit
                            break
                    self.log(f"    {name:<16} {pages} 页" + (f" / 上限 {lim}" if lim else "（不限）"))
            else:
                self.log(f"  今日云端 OCR 配额上限：{cfg.ocr.daily_page_limit} 页（分后端计）")
            if s["recent_failures"]:
                self.log("")
                self.log(f"  最近失败 {len(s['recent_failures'])} 条：")
                for f in s["recent_failures"][:10]:
                    self.log(f"    - {Path(f['path']).name}")
                    self.log(f"        {str(f['error'])[:120]}")
            self.log("=" * 74)

        self._start_job("正在统计…", job)

    def on_retry(self) -> None:
        cfg = self._prepare()
        if cfg is None:
            return

        def job() -> None:
            self.log("[引擎] 正在加载转换引擎…")
            store = StateStore(cfg.state_db)
            try:
                paths = store.retry_paths()
                if not paths:
                    self.log("没有需要重试的文件。")
                    return
                self.log(f"待重试 {len(paths)} 个文件（含上轮暂缓的）…\n")
                eng = self._new_engine(cfg, store, self.stop_event, self.log)
                ok = fail = 0
                total = len(paths)
                try:
                    for i, p in enumerate(paths, 1):
                        if self.stop_event.is_set():
                            self.log("\n[已停止] 剩余文件未处理。")
                            break
                        fp = Path(p)
                        self.events.put(("progress", i, total))
                        if not fp.exists():
                            store.mark_skipped(fp, 0, 0, "retry", "源文件已不存在")
                            self.log(f"[{i:>5}/{total}] --   源文件已不存在      {fp.name}")
                            continue
                        try:
                            t = eng.plan(fp)
                            if t is None:
                                self.log(f"[{i:>5}/{total}] --   不在转换范围内      {fp.name}")
                                continue
                            status, info = eng.process(t)
                            mark = {"ok": "OK  ", "skip": "--  ", "fail": "FAIL",
                                    "blocked": "BLK ", "defer": "HOLD"}.get(status, status)
                            self.log(f"[{i:>5}/{total}] {mark} {info:<22} {fp.name}")
                            ok += status == "ok"
                            fail += status == "fail"
                        except Exception as e:
                            fail += 1
                            self.log(f"[{i:>5}/{total}] FAIL {type(e).__name__}: {e}")
                finally:
                    eng.close()
                self.log("")
                self.log(f"重试完成：成功 {ok}，仍失败 {fail}")
            finally:
                store.close()

        self._start_job("正在重试失败文件…", job)

    def on_ping(self) -> None:
        cfg = self._prepare()
        if cfg is None:
            return

        def job() -> None:
            from doc2md.config import describe_credentials
            from doc2md.ocr_router import build_router

            self.log("=" * 74)
            self.log("  云端 OCR 后端自检")
            self.log("=" * 74)
            self.log(f"  后端链路 : {describe_backends(cfg)}")
            self.log(f"  凭据文件 : {describe_env_file(cfg)}")
            self.log("  后端凭据 :")
            for line in describe_credentials(cfg):
                self.log(f"    · {line}")
            self.log("")
            self.events.put(("pulse", True))
            router = build_router(cfg, store=None, verbose=True, logger=self.log)
            if router is None:
                self.log("[失败] 没有任何可用的云端 OCR 后端")
                self.log("       检查 config.json 的 ocr.backends，以及 .env 里的 Token 是否已填写。")
                return
            try:
                results = router.ping_all()
            finally:
                try:
                    router.close()
                except Exception:
                    pass
            ok_n = 0
            for name, ok, msg in results:
                ok_n += bool(ok)
                self.log(f"  [{'成功' if ok else '失败'}] {name:<16} {msg}")
            self.log("")
            self.log(f"  可用后端 {ok_n}/{len(results)}"
                     + ("，熔断切换链路已就绪。" if ok_n > 1 else
                        "，只有一个后端可用，无冗余切换能力。" if ok_n == 1 else "。"))
            self.log("=" * 74)

        self._start_job("正在检测云端 OCR…", job)

    def on_test(self) -> None:
        p = filedialog.askopenfilename(
            title="选择要单独转换的文件",
            filetypes=[("支持的文档", "*.docx *.doc *.pdf *.xlsx *.xls *.wps *.et *.html *.rtf"),
                       ("全部文件", "*.*")],
        )
        if not p:
            return
        target = Path(p)
        cfg = self._prepare()
        if cfg is None:
            return

        def job() -> None:
            self.log("[引擎] 正在加载转换引擎…")
            self.log("")
            self._banner(cfg, f"单文件测试：{target.name}")
            store = StateStore(cfg.state_db)
            eng = self._new_engine(cfg, store, self.stop_event, self.log)
            try:
                t = eng.plan(target)
                if t is None:
                    self.log(f"[跳过] 该格式不在转换范围内，或内容无法识别：{target.name}")
                    return
                self.log(f"  真实格式 : {t.kind.value}")
                self.log(f"  处理路线 : {t.route}")
                if t.pages:
                    self.log(f"  页数     : {t.pages}")
                if t.note:
                    self.log(f"  备注     : {t.note}")
                out_path = eng.md_path_for(target)
                self.log(f"  将输出到 : {out_path}")
                self.log("")
                status, info = eng.process(t)
                self.log("")
                self.log(f"  结果：{status}  ({info})")
                if out_path.exists():
                    text = out_path.read_text(encoding="utf-8-sig", errors="replace")
                    self.log(f"  输出：{out_path}")
                    self.log(f"  字数：{len(text)}")
                    self.log("  预览：")
                    for line in text.splitlines()[:12]:
                        self.log("    " + line[:88])
            finally:
                try:
                    eng.close()
                finally:
                    store.close()

        self._start_job(f"正在转换 {target.name}…", job)

    # ================= 退出 =================
    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno(
                APP_TITLE,
                "还有任务在运行。\n\n现在退出会中断它（已完成的文件已入库，重跑会自动跳过）。\n确定退出吗？",
            ):
                return
            self.stop_event.set()
            if self._svc is not None:
                try:
                    self._svc.stop()
                except Exception:
                    pass
        self._closing = True          # 先停掉 after 轮询，再销毁窗口
        try:
            self.root.destroy()
        except Exception:
            pass


def main(config_path: str | Path | None = None) -> int:
    _guard_stdio()
    try:                                  # 与命令行版一致：静音 pymupdf4llm 的编码噪音
        from doc2md.cli import _install_thread_guard

        _install_thread_guard()
    except Exception:
        pass
    _enable_dpi_awareness()

    try:
        root = tk.Tk()
    except Exception as e:
        print(f"[错误] 无法创建窗口（缺少图形环境？）：{e}")
        return 1

    try:
        Doc2MdApp(root, config_path)
    except Exception:
        traceback.print_exc()
        try:
            messagebox.showerror(APP_TITLE, "界面初始化失败：\n\n" + traceback.format_exc(limit=6))
        except Exception:
            pass
        return 1

    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
