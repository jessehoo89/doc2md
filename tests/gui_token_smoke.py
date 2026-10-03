# -*- coding: utf-8 -*-
"""「填写云端 OCR Token」对话框的端到端冒烟测试。

不需要人肉点界面：把对话框真的建出来、找到各个输入框、改一个值、
按「保存」，然后检查凭据文件与 os.environ 的结果。

**必须有桌面会话**（Tk 需要显示器）。拿不到显示器时自动跳过，不算失败。

## 用法

    .venv-gui\\Scripts\\python.exe tests/gui_token_smoke.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOG_PATH = Path(__file__).with_name("gui_token_smoke.log")

FAILURES = 0
SKIPPED = False

ORIGINAL_ENV_FILE = "# 手写注释：不要被冲掉\n" \
                    "# MinerU 的 Token 在下面\n" \
                    "DOC2MD_MINERU_TOKEN=sk-old-value\n" \
                    "\n" \
                    "MY_OWN_SETTING=keep-me\n"


class _Tee:
    def __init__(self, stream, path: Path):
        self._stream = stream
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "w", encoding="utf-8")

    def write(self, s):
        self._stream.write(s)
        self._file.write(s)
        return len(s)

    def flush(self):
        self._stream.flush()
        self._file.flush()


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILURES
    if cond:
        print(f"  [PASS] {name}")
    else:
        FAILURES += 1
        print(f"  [FAIL] {name}  {detail}")


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


def entries_of(win) -> list:
    from tkinter import ttk

    return [w for w in walk(win) if isinstance(w, ttk.Entry)]


def button_named(win, text: str):
    from tkinter import ttk

    for w in walk(win):
        if isinstance(w, ttk.Button):
            try:
                if w.cget("text") == text:
                    return w
            except Exception:
                continue
    return None


def labels_of(win) -> str:
    from tkinter import ttk

    out = []
    for w in walk(win):
        if isinstance(w, ttk.Label):
            try:
                out.append(str(w.cget("text")))
            except Exception:
                pass
    return "\n".join(out)


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="doc2md_gui_"))
    envp = tmp / ".env"
    envp.write_text(ORIGINAL_ENV_FILE, encoding="utf-8")

    # 隔离手段：给一份**临时 config.json**，让"生效的凭据文件"变成 tmp\.env。
    # 光设 DOC2MD_ENV_FILE 是没用的 —— load_config 优先看 config.json 同目录的
    # .env，那样本测试就会读写仓库里那份真实凭据。
    cfgp = tmp / "config.json"
    shutil.copy2(ROOT / "config.json", cfgp)

    os.environ["DOC2MD_NO_TOKEN_PROMPT"] = "1"
    for k in ("PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN", "DOC2MD_MINERU_TOKEN",
              "DOC2MD_SILICONFLOW_TOKEN", "DOC2MD_PADDLE_TOKEN"):
        os.environ.pop(k, None)

    try:
        root = tk.Tk()
    except Exception as e:                       # 没有桌面会话
        global SKIPPED
        SKIPPED = True
        print(f"  [跳过] 无法创建 Tk 窗口（{type(e).__name__}: {e}）")
        return 0
    root.withdraw()

    import gui

    app = gui.Doc2MdApp(root, cfgp)
    root.update()
    check("生效的凭据文件就是临时那份（没碰到仓库里的真 .env）",
          Path(app.config_path).parent == tmp)

    print("\n[1] 对话框能建出来，字段与 CRED_FIELDS 一一对应")
    # 不让 wait_window 阻塞：置成空实现，对话框会原地返回
    root.wait_window = lambda w=None: None
    app._on_tokens()

    tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
    check("弹出了一个设置窗口", len(tops) == 1, f"实际 {len(tops)} 个")
    if not tops:
        root.destroy()
        return 1
    win = tops[0]
    win.update_idletasks()

    ents = entries_of(win)
    order = [f.key for f in gui.CRED_FIELDS]
    check(f"输入框数量 = 凭据字段数（{len(gui.CRED_FIELDS)}）",
          len(ents) == len(gui.CRED_FIELDS), f"实际 {len(ents)} 个")

    text = labels_of(win)
    for f in gui.CRED_FIELDS:
        check(f"界面上能看到「{f.label}」", f.label in text)
    check("说明了保存后立即生效", "立即生效" in text, text[:120].replace("\n", " / "))
    check("列出了凭据文件路径", str(envp) in text)
    check("给出了当前填写情况", "填写情况" in text)

    print("\n[2] 高级选项默认折叠，勾上后能展开")
    from tkinter import ttk

    advanced_keys = [f.key for f in gui.CRED_FIELDS if f.advanced]
    adv_entries = [ents[order.index(k)] for k in advanced_keys]
    # 用「分组框有没有被挂到布局上」判断，而不是 winfo_ismapped ——
    # 本测试里主窗口是 withdraw 的，子控件本来就不算 mapped，判断不出展开与否。
    adv_frame = win.nametowidget(adv_entries[0].winfo_parent())
    check("高级分组默认没挂到布局上（用户看不到）",
          adv_frame.winfo_manager() == "", repr(adv_frame.winfo_manager()))
    chk = None
    for w in walk(win):
        if isinstance(w, ttk.Checkbutton) and "显示高级选项" in str(w.cget("text")):
            chk = w
            break
    check("找得到「显示高级选项」开关", chk is not None)
    if chk is not None:
        chk.invoke()
        win.update()
        check("勾上之后高级分组挂上了", adv_frame.winfo_manager() == "grid",
              repr(adv_frame.winfo_manager()))
        chk.invoke()
        win.update()
        check("取消勾选后又摘下来", adv_frame.winfo_manager() == "",
              repr(adv_frame.winfo_manager()))

    print("\n[3] 现有值被预填进输入框（含来自 .env 的那个）")
    prefilled = [e.get() for e in ents]
    check("MinerU 的旧值已预填", "sk-old-value" in prefilled, repr(prefilled))

    print("\n[4] 改一个值 → 按「保存」")
    # 输入框顺序 = CRED_FIELDS 顺序（先常用后高级，与构建顺序一致）
    idx = order.index("DOC2MD_MINERU_TOKEN")
    ents[idx].delete(0, "end")
    ents[idx].insert(0, "sk-new-value")

    btn = button_named(win, "保存")
    check("找得到「保存」按钮", btn is not None)
    if btn is None:
        root.destroy()
        return 1
    btn.invoke()
    root.update()

    after = envp.read_text(encoding="utf-8")
    print("\n[5] 文件与生效状态")
    check("新值写进了凭据文件", "DOC2MD_MINERU_TOKEN=sk-new-value" in after, after)
    check("旧值已不在", "sk-old-value" not in after)
    check("手写注释保留", "# 手写注释：不要被冲掉" in after)
    check("用户自己的键保留", "MY_OWN_SETTING=keep-me" in after)
    check("没有残留 .tmp", not envp.with_name(".env.tmp").exists())
    check("os.environ 立即生效（未重启）",
          os.environ.get("DOC2MD_MINERU_TOKEN") == "sk-new-value",
          repr(os.environ.get("DOC2MD_MINERU_TOKEN")))
    check("窗口已关闭", not win.winfo_exists())

    print("\n[6] 再填一个之前没有的键（PaddleOCR）")
    app._on_tokens()
    tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
    if tops:
        win2 = tops[0]
        win2.update_idletasks()
        ents2 = entries_of(win2)
        idx2 = order.index("PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN")
        ents2[idx2].insert(0, "paddle-token-123")
        b2 = button_named(win2, "保存")
        if b2 is not None:
            b2.invoke()
            root.update()
        after2 = envp.read_text(encoding="utf-8")
        check("新增的键写进了文件", "PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN=paddle-token-123" in after2,
              after2)
        check("原有的键没被动", "DOC2MD_MINERU_TOKEN=sk-new-value" in after2)
        check("注释依然在", "# MinerU 的 Token 在下面" in after2)

    print("\n[7] 没填过 Token 时自动弹窗；点「稍后再说」之后不再打扰")
    # 模拟装机后的样子：.env 存在（安装包从 .env.example 复制出来的），但三项都空着
    os.environ.pop("DOC2MD_ENV_FILE", None)
    for f in gui.CRED_FIELDS:
        if not f.advanced:
            os.environ.pop(f.key, None)
    blank = ("# 云端 OCR 凭据文件\n\n"
             "DOC2MD_MINERU_TOKEN=\n"
             "PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN=\n"
             "DOC2MD_SILICONFLOW_TOKEN=\n")
    envp.write_text(blank, encoding="utf-8")
    os.environ.pop("DOC2MD_NO_TOKEN_PROMPT", None)   # 放开前面为省事设的开关

    def wait_for_dialog(seconds: float) -> list:
        deadline = time.time() + seconds
        while time.time() < deadline:
            root.update()
            hit = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
            if hit:
                return hit
            time.sleep(0.05)
        return []

    app2 = gui.Doc2MdApp(root, cfgp)          # 与装机后第一次打开同一条路径
    found = wait_for_dialog(6)
    check("启动后自动弹出了设置窗口（无需用户去翻菜单）", bool(found), "6 秒内没等到")
    check("弹窗没有擅自改动凭据文件", envp.read_text(encoding="utf-8") == blank)
    if found:
        win3 = found[0]
        win3.update_idletasks()
        b3 = button_named(win3, "稍后再说")
        check("有「稍后再说」可以跳过", b3 is not None)
        if b3 is not None:
            b3.invoke()
            root.update()
        check("点掉之后窗口关闭", not win3.winfo_exists())
        check("把「以后再说」记在了凭据文件的注释里（不额外造文件）",
              "# DOC2MD_TOKEN_PROMPT=skipped" in envp.read_text(encoding="utf-8"),
              envp.read_text(encoding="utf-8"))
        import doc2md.config as _cfg
        _cfg.load_env_file(envp, force=True)
        check("注释行不会被当成键读进来",
              not (os.environ.get("DOC2MD_TOKEN_PROMPT") or "").strip())

        print("\n[8] 下次启动不再自动弹（用户已经明确说过以后再说）")
        app3 = gui.Doc2MdApp(root, cfgp)
        check("没有再次弹出", not wait_for_dialog(3), "3 秒后仍弹出了窗口")

        print("\n[9] 菜单里仍可主动打开，且这次取消不会再写记号")
        before = envp.read_text(encoding="utf-8")
        app3._on_tokens()                      # first_run=False
        tops3 = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
        if tops3:
            b4 = button_named(tops3[0], "稍后再说")
            if b4 is not None:
                b4.invoke()
                root.update()
        check("文件内容没被这段操作改动", envp.read_text(encoding="utf-8") == before)

    root.destroy()
    return 0


def main() -> int:
    sys.stdout = _Tee(sys.stdout, LOG_PATH)
    sys.stderr = sys.stdout
    print("=" * 74)
    print("  「填写云端 OCR Token」对话框冒烟")
    print("=" * 74)
    try:
        run()
    except Exception:
        import traceback
        traceback.print_exc()
        globals()["FAILURES"] = FAILURES + 1
    print()
    print("=" * 74)
    if SKIPPED:
        print("  已跳过（无桌面会话）")
    else:
        print(f"  失败项：{FAILURES}")
    print(f"  日志：{LOG_PATH}")
    print("=" * 74)
    sys.stdout.flush()
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
