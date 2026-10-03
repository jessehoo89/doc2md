# -*- coding: utf-8 -*-
"""「设置」窗口（含云端 OCR Token 页）的端到端冒烟测试。

不需要人肉点界面：把设置窗口真的建出来、切到 Token 页、找到各个输入框、
改一个值、按「保存」，然后检查凭据文件与 os.environ 的结果。
另外**专门盯住那个回归**：内容区必须能滚，而「保存」按钮必须在滚动区之外
（旧版「填写 Token」小窗口勾上高级选项后，按钮被顶出屏幕且无处可滚）。

**必须有桌面会话**（Tk 需要显示器）。拿不到显示器时自动跳过，不算失败。

## 用法

    .venv-gui\\Scripts\\python.exe tests/gui_token_smoke.py
"""
from __future__ import annotations

import json
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

TOKEN_TAB = "Token"          # 页签名片段，够 _select_tab / 本测试定位使用

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


def ancestors(widget) -> list:
    """控件的祖先链（不含自身）。用来判断「这个按钮在不在滚动区里面」。"""
    out = []
    try:
        parent = widget.winfo_parent()
        while parent:
            node = widget.nametowidget(parent)
            out.append(node)
            parent = node.winfo_parent()
    except Exception:
        pass
    return out


def ancestor_kinds(widget) -> list:
    return [type(w).__name__ for w in ancestors(widget)]


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


def tab_holder(win, title_part: str):
    """找到 Notebook 里标题含 title_part 的那一页，返回它的容器 Frame。

    必须**按页签限定范围**再找控件：设置窗口有好几个页签，全局 walk 会把
    别的页签的 Entry 也算进来（凭据输入框数量就对不上了）。
    """
    from tkinter import ttk

    for w in walk(win):
        if isinstance(w, ttk.Notebook):
            for i in range(w.index("end")):
                if title_part in str(w.tab(i, "text")):
                    return w.nametowidget(w.tabs()[i])
    return None


def sole_toplevel(root) -> list:
    return [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]


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

    print("\n[1] 设置窗口能建出来，Token 页字段与 CRED_FIELDS 一一对应")
    # 不让 wait_window 阻塞：置成空实现，窗口会原地返回
    root.wait_window = lambda w=None: None
    app._on_settings(TOKEN_TAB)

    tops = sole_toplevel(root)
    check("弹出了一个设置窗口", len(tops) == 1, f"实际 {len(tops)} 个")
    if not tops:
        root.destroy()
        return 1
    win = tops[0]
    win.update_idletasks()

    holder = tab_holder(win, TOKEN_TAB)
    check("找得到「云端 OCR Token」页签", holder is not None)
    if holder is None:
        root.destroy()
        return 1

    order = [f.key for f in gui.CRED_FIELDS]
    ents = entries_of(holder)
    check(f"Token 页输入框数量 = 凭据字段数（{len(gui.CRED_FIELDS)}）",
          len(ents) == len(gui.CRED_FIELDS), f"实际 {len(ents)} 个")

    text = labels_of(win)
    for f in gui.CRED_FIELDS:
        check(f"界面上能看到「{f.label}」", f.label in text)
    check("说明了保存后立即生效", "立即生效" in text, text[:120].replace("\n", " / "))
    check("列出了凭据文件路径", str(envp) in text)
    check("给出了当前填写情况", "填写情况" in text)

    print("\n[2] 回归项：内容可滚 + 「保存」永远在滚动区之外 + 高级项常显")
    from tkinter import ttk

    canvases = [w for w in walk(holder) if isinstance(w, tk.Canvas)]
    check("Token 页内容区套了画布（可滚动）", len(canvases) == 1,
          f"实际 {len(canvases)} 个 Canvas")
    sbs = [w for w in walk(holder) if isinstance(w, ttk.Scrollbar)]
    check("Token 页带纵向滚动条", len(sbs) >= 1, f"实际 {len(sbs)} 个")

    save_btn = button_named(win, "保存")
    check("找得到「保存」按钮", save_btn is not None)
    if save_btn is not None:
        kinds = ancestor_kinds(save_btn)
        check("「保存」按钮**不在**滚动区里（内容再长也不会被顶出屏幕）",
              "Canvas" not in kinds, " → ".join(kinds))

    adv_keys = [f.key for f in gui.CRED_FIELDS if f.advanced]
    check("高级项默认就显示（不再需要勾选展开）",
          all(k in order and order.index(k) < len(ents) for k in adv_keys))
    if adv_keys:
        adv_ent = ents[order.index(adv_keys[0])]
        check("高级字段在滚动区内（内容长了能滚到它）",
              "Canvas" in ancestor_kinds(adv_ent),
              " → ".join(ancestor_kinds(adv_ent)))

    check("窗口可缩放（小屏用户可以自己拉大）", tuple(win.resizable()) != (0, 0),
          str(win.resizable()))

    print("\n[3] 现有值被预填进输入框（含来自 .env 的那个）")
    prefilled = [e.get() for e in ents]
    check("MinerU 的旧值已预填", "sk-old-value" in prefilled, repr(prefilled))

    print("\n[4] 改一个值 → 按「保存」")
    # 输入框顺序 = CRED_FIELDS 顺序（先常用后高级，与构建顺序一致）
    idx = order.index("DOC2MD_MINERU_TOKEN")
    ents[idx].delete(0, "end")
    ents[idx].insert(0, "sk-new-value")

    if save_btn is None:
        root.destroy()
        return 1
    save_btn.invoke()
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
    # 同一趟里也会把常规配置写回 config.json —— 顺带确认没写坏
    import json as _json
    try:
        saved = _json.loads(cfgp.read_text(encoding="utf-8"))
        check("config.json 仍然是合法 JSON", isinstance(saved, dict))
        check("保存没有丢掉其它配置键", "roots" in saved and "output" in saved)
        check("pdf_engine 归一化后仍是允许值",
              saved.get("pdf_engine") in ("rule", "layout"),
              repr(saved.get("pdf_engine")))
    except Exception as e:
        check("config.json 仍然是合法 JSON", False, f"{type(e).__name__}: {e}")

    print("\n[6] 再填一个之前没有的键（PaddleOCR）")
    app._on_settings(TOKEN_TAB)
    tops = sole_toplevel(root)
    if tops:
        win2 = tops[0]
        win2.update_idletasks()
        holder2 = tab_holder(win2, TOKEN_TAB)
        ents2 = entries_of(holder2)
        idx2 = order.index("PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN")
        ents2[idx2].insert(0, "paddle-token-123")
        b2 = button_named(win2, "保存")
        if b2 is not None:
            b2.invoke()
            root.update()
        after2 = envp.read_text(encoding="utf-8")
        check("新增的键写进了文件",
              "PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN=paddle-token-123" in after2, after2)
        check("原有的键没被动", "DOC2MD_MINERU_TOKEN=sk-new-value" in after2)
        check("注释依然在", "# MinerU 的 Token 在下面" in after2)

    print("\n[6b] 引擎选档：界面上切 layout → 存进 config.json → 加载回来生效")
    def radio_in(holder, value: str):
        for w in walk(holder):
            if isinstance(w, ttk.Radiobutton):
                try:
                    if str(w.cget("value")) == value:
                        return w
                except Exception:
                    continue
        return None

    def set_engine_and_save(target: str) -> str:
        app._on_settings("转换与引擎")
        top = sole_toplevel(root)
        if not top:
            return "(没弹出窗口)"
        w = top[0]
        w.update_idletasks()
        page = tab_holder(w, "转换与引擎")
        rb = radio_in(page, target) if page is not None else None
        if rb is None:
            return "(找不到取值为 %s 的单选钮)" % target
        rb.invoke()
        w.update_idletasks()
        b = button_named(w, "保存")
        if b is None:
            return "(找不到保存按钮)"
        b.invoke()
        root.update()
        import json as _j
        return str(_j.loads(cfgp.read_text(encoding="utf-8")).get("pdf_engine"))

    got_layout = set_engine_and_save("layout")
    check("界面切到 layout 后 config.json 就是 layout",
          got_layout == "layout", got_layout)
    from doc2md.config import load_config as _load_cfg
    got_cfg = _load_cfg(cfgp).pdf_engine
    check("重新加载后引擎也跟着变（界面→配置→逻辑这条链通了）",
          got_cfg == "layout", got_cfg)
    got_back = set_engine_and_save("rule")
    check("再切回 rule 也生效", got_back == "rule", got_back)
    check("主界面 app.cfg 同步到了 rule", app.cfg.pdf_engine == "rule",
          str(getattr(app.cfg, "pdf_engine", None)))

    print("\n[7] 没填过 Token 时自动打开设置窗口的 Token 页；关掉后不再打扰")
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
            hit = sole_toplevel(root)
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
        nb3 = next((w for w in walk(win3) if isinstance(w, ttk.Notebook)), None)
        if nb3 is not None:
            cur = str(nb3.tab(nb3.select(), "text"))
            check("自动打开时定位在「云端 OCR Token」页", TOKEN_TAB in cur, cur)
        b3 = button_named(win3, "稍后再说")
        check("首启时取消按钮显示为「稍后再说」", b3 is not None)
        if b3 is not None:
            b3.invoke()
            root.update()
        check("点掉之后窗口关闭", not win3.winfo_exists())
        txt3 = envp.read_text(encoding="utf-8")
        check("把「以后再说」记在了凭据文件的注释里（不额外造文件）",
              "# DOC2MD_TOKEN_PROMPT=skipped" in txt3, txt3)
        import doc2md.config as _cfg
        _cfg.load_env_file(envp, force=True)
        check("注释行不会被当成键读进来",
              not (os.environ.get("DOC2MD_TOKEN_PROMPT") or "").strip())

        print("\n[8] 下次启动不再自动弹；菜单里仍可主动打开（首页已无 Token 入口）")
        app3 = gui.Doc2MdApp(root, cfgp)      # 与装机后第二次打开同一条路径
        check("没有再次弹出", not wait_for_dialog(3), "3 秒后仍弹出了窗口")

        before = envp.read_text(encoding="utf-8")
        app3._on_settings(TOKEN_TAB)
        tops3 = sole_toplevel(root)
        if tops3:
            w3 = tops3[0]
            w3.update_idletasks()
            b4 = button_named(w3, "放弃改动")
            check("主动打开时按钮是「放弃改动」（不是首启，不写记号）",
                  b4 is not None)
            if b4 is not None:
                b4.invoke()
                root.update()
        check("文件内容没被这段操作改动", envp.read_text(encoding="utf-8") == before)
        check("主界面已无「填写云端 OCR Token…」按钮",
              button_named(app3.root, "填写云端 OCR Token…") is None)

        print("\n[9] 首页版式：左侧不再重复放按钮，摘要栏已撤，重载按钮挪到顶部")
        # ⚠ 本测试建了 app / app2 / app3 三个实例，它们**共用同一个 Tk root**，
        # 各自的首页控件都挂在同一棵树里。所以这里一律按 app3 自己的容器定位
        # （_toolbar / _cfg_inner），不能 button_named(app3.root, …) —— 那样会
        # 命中第一个实例的按钮，invoke 到别的 app 上（踩过，表现是"点了没反应"）。
        from tkinter import ttk as _ttk

        tbar = app3._toolbar
        texts = [str(w.cget("text")) for w in tbar.winfo_children()
                 if isinstance(w, _ttk.Button)]
        check("顶部按钮栏有「设置…」", "设置…" in texts, str(texts))
        check("「重新加载配置」在同一个按钮栏里（＝已从左侧面板搬走）",
              "重新加载配置" in texts, str(texts))
        if "设置…" in texts and "重新加载配置" in texts:
            i = texts.index("设置…")
            check("它就紧跟在「设置…」右边",
                  i + 1 < len(texts) and texts[i + 1] == "重新加载配置",
                  f"{texts[i:i + 3]}（整栏 {texts}）")

        panel = app3._cfg_inner
        for gone in ("保存配置", "打开配置文件", "打开凭据文件", "打开程序目录",
                     "设置…（含云端 OCR Token）"):
            check(f"左侧面板不再有「{gone}」按钮",
                  button_named(panel, gone) is None)
        check("「当前生效配置」摘要栏已撤掉",
              "当前生效配置" not in labels_of(panel))

        # 按钮栏是单行 pack，窗口比它窄时**不换行、直接被切掉**（实测最右边那颗
        # 曾经在最小窗口下完全看不见）—— 所以最小宽度必须跟着按钮栏走。
        need = app3._toolbar.winfo_reqwidth()
        mw = app3.root.minsize()[0]
        cap = app3.root.winfo_screenwidth()
        check(f"窗口最小宽度 {mw} ≥ 按钮栏需求 {min(need, cap)}（末按钮不会被切掉）",
              mw >= min(need, cap), f"min={mw} need={need} screen={cap}")

        # 左侧只剩三组配置后，竖向滚动条在默认窗口下应自动收起（内容超高时才回来）。
        holder = app3._cfg_canvas.master
        vbars = [w for w in holder.winfo_children() if isinstance(w, _ttk.Scrollbar)]
        check("左侧竖向滚动条：内容放得下时自动隐藏",
              bool(vbars) and not vbars[0].winfo_ismapped(),
              f"找到 {len(vbars)} 条，ismapped="
              f"{[bool(w.winfo_ismapped()) for w in vbars]}")

        # 搬走的按钮别只验位置，还要验它真的还能用：外部改盘上的 config.json，
        # 点一下应该重新读进来并同步到左侧列表。
        reload_btn = next((w for w in tbar.winfo_children()
                           if isinstance(w, _ttk.Button)
                           and str(w.cget("text")) == "重新加载配置"), None)
        if reload_btn is not None:
            data = json.loads(cfgp.read_text(encoding="utf-8"))
            probe = str(tmp / "重载探针目录")
            data["roots"] = [probe]
            cfgp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            before_roots = list(app3.lb_roots.get(0, "end"))
            reload_btn.invoke()
            root.update()
            shown = list(app3.lb_roots.get(0, "end"))
            check("点它能把磁盘上的 config.json 重新读进左侧面板",
                  shown == [probe], f"{before_roots} → {shown}")

    root.destroy()
    return 0


def main() -> int:
    sys.stdout = _Tee(sys.stdout, LOG_PATH)
    sys.stderr = sys.stdout
    print("=" * 74)
    print("  「设置」窗口（云端 OCR Token 页）冒烟")
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
