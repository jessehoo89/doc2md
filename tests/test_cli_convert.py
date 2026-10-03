#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""转换清单（`doc2md convert`）的解析与驱动。

## 为什么值得单独测

这个功能的失败方式和别处不同：**清单里写错一个字符不会报错**，只会安静地少转
一份文件 —— 等发现的时候 md 已经进了库。所以本测试的重点不是"能不能转"，而是：

  1. 各种真实来源的清单都能读对 —— `dir /b > list.txt` 出来的是 GBK、手工另存的
     常带 UTF-8 BOM、从 Excel 粘来的带 Tab 列、资源管理器"复制为路径"带引号；
  2. **找不到的条目必须被报出来**，不能被静默丢掉；
  3. 相对路径要能相对**清单文件所在目录**解析（清单和文件放一起是最自然的用法）；
  4. `convert` 真的只处理清单里的文件、与 `cfg.roots` 无关 —— 这是整个功能的地基；
  5. `--force` 要真的强制重转（踩过：清了状态库记录，却被"md 比源文件新就不覆盖"
     那道保护拦住，用户看到的是"加了 --force 还是没重转"）；
  6. 汇总横幅不许引用不存在的配置字段（踩过：`cfg.pdf_rule_text_tables` 在 config
     里根本没有，`pdf_engine=rule` 时 CLI 一启动就 AttributeError）。

## 用法（离线，用到 pymupdf 造测试 PDF）

    .venv\\Scripts\\python.exe tests\\test_cli_convert.py
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from doc2md import filelist                       # noqa: E402
from doc2md.cli import (                          # noqa: E402
    _banner,
    _install_thread_guard,
    build_parser,
    cmd_convert,
)
from doc2md.config import load_config              # noqa: E402
from doc2md.state import StateStore                # noqa: E402

# pymupdf 的 ONNX 子进程在中文 Windows 下会往 stderr 抛无害的解码报错，
# 不装这个钩子的话测试输出会被它刷屏（不影响结果，但很误导）。
_install_thread_guard()

PASS = 0
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS
    if ok:
        PASS += 1
        print(f"  [通过] {name}")
    else:
        FAILS.append(f"{name}｜{detail}")
        print(f"  [失败] {name}　{detail}")
    return ok


def silent(fn, *a, **kw):
    """跑一个会往 stdout 大量输出的动作，返回 (结果, 输出文本)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **kw)
    return r, buf.getvalue()


# --------------------------------------------------------------------------- #
print("=" * 74)
print("  转换清单（doc2md convert）")
print("=" * 74)

TMP = Path(tempfile.mkdtemp(prefix="doc2md_cl_"))


# ---- 夹具 ------------------------------------------------------------------- #
def make_layout() -> tuple[Path, Path]:
    """造一个「语料目录 + 清单 + 空 roots + 输出目录」的临时现场。"""
    base = TMP / "现场"
    (base / "语料").mkdir(parents=True, exist_ok=True)
    (base / "roots空").mkdir(parents=True, exist_ok=True)
    (base / "out").mkdir(parents=True, exist_ok=True)
    for name in ("甲.docx", "乙.docx"):
        (base / "语料" / name).write_bytes(b"placeholder")
    (base / "语料" / "无关.txt").write_bytes(b"placeholder")      # 格式不支持
    (base / "语料" / "~$临时.docx").write_bytes(b"placeholder")   # Word 临时文件
    return base, base / "语料"


def make_config(base: Path, *, engine: str = "rule", roots=None,
                name: str = "config.json") -> Path:
    """写一份临时配置。name 要区分开 —— 几个用例各用各的文件，
    否则后写的会覆盖先写的，先加载的那份 CFGP 就悄悄换了个档。"""
    cfgp = base / name
    cfgp.write_text(json.dumps({
        "roots": [str(r) for r in (roots if roots is not None else [base / "roots空"])],
        "watch_extensions": [".doc", ".docx", ".wps", ".xls", ".xlsx", ".et", ".pdf"],
        "keep_original": True,
        "output": {"mode": "custom", "root": str(base / "out"),
                   "layout": "mirror", "on_collision": "overwrite"},
        "state_db": str(base / "state.db"),
        "log_dir": str(base / "logs"),
        "pdf_engine": engine,
        "ocr": {"enabled": False},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfgp


def write_cn_pdf(path: Path) -> None:
    """造一份带文字层的 1 页中文 PDF（rule 档能正常提取）。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "关于加强安全生产工作的通知",
                     fontname="china-s", fontsize=18)
    body = ("各部门要认真落实安全生产责任制，全面排查治理各类事故隐患，"
            "坚决遏制重特大事故发生。")
    for i in range(6):
        page.insert_text((72, 140 + i * 20), body, fontname="china-s", fontsize=11)
    doc.save(path)
    doc.close()


BASE, CORPUS = make_layout()
CFGP = make_config(BASE)
CFG = load_config(CFGP)


# --------------------------------------------------------------------------- #
print("\n[1] 清单文件的行容错（注释 / 空行 / 引号 / Tab / 首尾空白 / 重复）")
manifest = BASE / "清单.txt"
# 用 write_bytes：Windows 下 write_text 会把 \n 再展开成 \r\n，
# 我手写的 \r\n 会变成 \r\r\n，读回来行数翻倍（第一次就踩到了）。
manifest.write_bytes("\r\n".join([
    "# 这行是注释，应被忽略",
    "",
    "  语料\\甲.docx  ",
    '"语料\\乙.docx"',
    "语料\\乙.docx\t备注列\t多余列",
    "缺失.docx",
    "语料\\无关.txt",
]).encode("utf-8"))

lines, enc = filelist.read_manifest(str(manifest))
check("读清单不抛异常且拿到 7 行", len(lines) == 7, f"{len(lines)} 行")
res = filelist.collect(lines, cfg=CFG, base_dir=manifest.parent, source=str(manifest))
names = sorted(p.name for p in res.paths)
check("注释/空行都不会混进结果（只剩 2 个可转文件）",
      names == sorted(["甲.docx", "乙.docx"]), str(names))
check("首尾空白与引号被剥掉", all(str(p.parent).endswith("语料") for p in res.paths),
      str([str(p) for p in res.paths]))
check("重复条目只算一次，但计数如实报出", res.duplicates == 1, str(res.duplicates))
check("找不到的条目被报出来（不是静默丢弃）", res.missing == ["缺失.docx"],
      str(res.missing))
check("存在但扩展名不在范围内的单独归类",
      [p.name for p in res.unsupported] == ["无关.txt"],
      str([p.name for p in res.unsupported]))
check("CRLF 清单不会在路径尾部留下 \\r",
      not any("\r" in str(p) for p in res.paths), str([str(p) for p in res.paths]))
check("ok 为真（有可转文件）", res.ok)
check("摘要包含关键计数", "去重" in res.summary() and "找不到" in res.summary(),
      res.summary())


# --------------------------------------------------------------------------- #
print("\n[2] 清单文件的编码探测（GBK / UTF-8 / BOM / UTF-16）")
for enc_name, expect in (("utf-8", "utf-8"), ("utf-8-sig", "utf-8-sig"),
                         ("gbk", "gbk"), ("utf-16", "utf-16")):
    p = BASE / f"清单_{enc_name}.txt"
    p.write_bytes("语料\\甲.docx\n".encode(enc_name))
    got_lines, got_enc = filelist.read_manifest(str(p))
    one = filelist.collect(got_lines, cfg=CFG, base_dir=p.parent)
    check(f"{enc_name:<10} 解出正确编码名并找到文件",
          got_enc == expect and [x.name for x in one.paths] == ["甲.docx"],
          f"enc={got_enc!r} paths={[x.name for x in one.paths]}")

# 无 BOM 的 UTF-8 不该被报成 utf-8-sig（只是名字，但会让人以为有 BOM）
plain = BASE / "清单_无BOM.txt"
plain.write_bytes("语料\\甲.docx\n".encode("utf-8"))
check("无 BOM 的 UTF-8 报告为 utf-8", filelist.read_manifest(str(plain))[1] == "utf-8",
      filelist.read_manifest(str(plain))[1])


# --------------------------------------------------------------------------- #
print("\n[3] 目录行递归展开（复用根目录扫描的过滤规则）")
dird = filelist.collect([str(CORPUS)], cfg=CFG)
dnames = sorted(p.name for p in dird.paths)
check("展开出 2 个文档（.txt 与 ~$ 临时文件都被滤掉）",
      dnames == sorted(["甲.docx", "乙.docx"]), str(dnames))
check("from_dirs 记下了展开来源", dird.from_dirs == 2, str(dird.from_dirs))


# --------------------------------------------------------------------------- #
print("\n[4] 空清单 / 全是坏条目时 ok 为假")
empty = filelist.collect(["# 只有注释", ""], cfg=CFG)
check("没解析出任何文件时 ok 为假", not empty.ok, str(empty.paths))
only_bad = filelist.collect(["不存在.docx"], cfg=CFG)
check("只有缺失项时 ok 也为假", not only_bad.ok and only_bad.missing,
      f"paths={only_bad.paths} missing={only_bad.missing}")


# --------------------------------------------------------------------------- #
print("\n[5] 汇总横幅不得引用不存在的配置字段")
# cli.py 里的 `cfg` 一定是 Config（没有局部别名），逐字段核对零假阳性。
src = (ROOT / "doc2md" / "cli.py").read_text(encoding="utf-8")
used = sorted(set(re.findall(r"\bcfg\.([a-zA-Z_][a-zA-Z0-9_]*)", src)))
ghosts = [n for n in used if not hasattr(CFG, n)]
check("cli.py 引用的 cfg.X 都真实存在", not ghosts, f"幽灵字段：{ghosts}")

for engine in ("rule", "layout"):
    cfgp = make_config(BASE, engine=engine, name=f"cfg_{engine}.json")
    one_cfg = load_config(cfgp)
    _, out = silent(_banner, one_cfg, "自检")
    shown = [l for l in out.splitlines() if "文本 PDF" in l]
    want = "rule 档" if engine == "rule" else "layout 档"
    check(f"_banner 在 pdf_engine={engine} 下正常且播报正确",
          bool(shown) and want in shown[0], str(shown))


# --------------------------------------------------------------------------- #
print("\n[6] 命令行参数解析")
parser = build_parser()
a = parser.parse_args(["convert", "a.docx", "--list", "x.txt", "--dry-run", "--force"])
check("位置路径 / --list / --dry-run / --force 都认得",
      a.paths == ["a.docx"] and a.list == "x.txt" and a.dry_run and a.force,
      f"{a.paths} {a.list} {a.dry_run} {a.force}")
check("-l 是 --list 的短选项", parser.parse_args(["convert", "-l", "-"]).list == "-")
check("convert 不需要位置参数（可以只用 --list）",
      parser.parse_args(["convert", "--list", "x.txt"]).paths == [])
cli_src = (ROOT / "doc2md" / "cli.py").read_text(encoding="utf-8")
check("run 的帮助里指路了 convert",
      re.search(r'add\("run",\s*"[^"]*convert', cli_src) is not None,
      "run 的 help 未提到 convert")


# --------------------------------------------------------------------------- #
print("\n[7] 清单驱动：只转点名的文件，与 cfg.roots 无关")
pdf = CORPUS / "通知.pdf"
write_cn_pdf(pdf)

args = argparse.Namespace(config=str(CFGP), root=None, limit=0, quiet=True,
                          no_ocr=True, paths=[str(pdf)], list=None,
                          dry_run=False, force=False)
rc, out = silent(cmd_convert, args, load_config(CFGP))
check("退出码 0", rc == 0, str(rc))
check("确实处理了清单里的文件（成功 1）", "成功 1" in out,
      [l for l in out.splitlines() if "成功" in l])
# 这个 PDF 在 BASE\语料 下，而这份配置的 roots 是空目录 → 算不出相对路径，
# 于是按文件名平铺到输出根。这是设计内的行为，但必须**明确提示**出来。
md = BASE / "out" / "通知.md"
check("md 落地且非空（不在 roots 下时按文件名平铺）",
      md.exists() and md.stat().st_size > 0,
      f"{md} exists={md.exists()}")
check("平铺这件事有明确提示，不是让用户自己发现",
      "会平铺到输出根目录" in out,
      [l for l in out.splitlines() if "提示" in l])
check("roots 是空目录也照样处理（证明没去扫 roots）",
      "按给定清单处理" in out, out[:200])

# 造一个 roots 里有内容、但清单里没有的对照：证明它不会顺手扫 roots
stray = BASE / "roots空" / "不该被转.docx"
stray.write_bytes(b"placeholder")
args2 = argparse.Namespace(config=str(CFGP), root=None, limit=0, quiet=True,
                           no_ocr=True, paths=[str(pdf)], list=None,
                           dry_run=False, force=False)
_, out2 = silent(cmd_convert, args2, load_config(CFGP))
probe = StateStore(BASE / "state.db")
check("再次运行幂等跳过清单里的文件", "成功 0" in out2 and "跳过 1" in out2,
      [l for l in out2.splitlines() if "成功" in l])
check("roots 里的文件没被顺带处理（状态库里查不到）",
      probe.get(stray) is None, "roots 被扫了，说明没走纯清单路径")
probe.close()
stray.unlink()

# roots 覆盖到文件所在目录后，同一次转换就会按 mirror 保住下层的目录结构 ——
# 所以"平铺"取决于 roots，而不是清单模式本身的固有行为。
cfg_b = make_config(BASE, roots=[BASE], name="config_mirror.json")
args_b = argparse.Namespace(config=str(cfg_b), root=None, limit=0, quiet=True,
                            no_ocr=True, paths=[str(pdf)], list=None,
                            dry_run=False, force=True)
_, out_b = silent(cmd_convert, args_b, load_config(cfg_b))
md_b = BASE / "out" / "语料" / "通知.md"
check("roots 覆盖到文件所在目录后，md 保留下层结构",
      md_b.exists() and md_b.stat().st_size > 0, f"{md_b} exists={md_b.exists()}")
check("此时不再有平铺提示", "会平铺" not in out_b,
      [l for l in out_b.splitlines() if "平铺" in l])


# --------------------------------------------------------------------------- #
print("\n[8] --force 要真的强制重转（不能被「md 较新」的保护拦住）")
# 先清记录 —— 否则幂等跳过会先命中，测不到「md 比源文件新就不覆盖」那道保护
cleaner = StateStore(BASE / "state.db")
cleaner.forget([pdf])
cleaner.close()

md.write_text("人工改过的内容，别覆盖我", encoding="utf-8")
args3 = argparse.Namespace(config=str(CFGP), root=None, limit=0, quiet=True,
                           no_ocr=True, paths=[str(pdf)], list=None,
                           dry_run=False, force=False)
_, out3 = silent(cmd_convert, args3, load_config(CFGP))
check("不加 --force 时被「已有较新 md」拦住",
      "已有较新 md" in out3 or "跳过 1" in out3,
      [l for l in out3.splitlines() if "成功" in l or "跳过" in l])
check("不加 --force 时保留人工改动",
      md.read_text(encoding="utf-8") == "人工改过的内容，别覆盖我",
      md.read_text(encoding="utf-8")[:40])

args4 = argparse.Namespace(config=str(CFGP), root=None, limit=0, quiet=True,
                           no_ocr=True, paths=[str(pdf)], list=None,
                           dry_run=False, force=True)
rc4, out4 = silent(cmd_convert, args4, load_config(CFGP))
check("加 --force 后真的重转（成功 1）", rc4 == 0 and "成功 1" in out4,
      [l for l in out4.splitlines() if "成功" in l])
check("人工改动被覆盖（--force 名副其实）",
      md.read_text(encoding="utf-8") != "人工改过的内容，别覆盖我",
      md.read_text(encoding="utf-8")[:40])


# --------------------------------------------------------------------------- #
print("\n[9] state.forget：只删指定文件的记录")
store = StateStore(BASE / "probe.db")
store.mark_ok(pdf, 1, 1.0, "pdf-text", str(md))
store.mark_ok(CORPUS / "甲.docx", 1, 1.0, "docx", "x.md")
check("forget 命中 1 条", store.forget([pdf]) == 1)
check("目标记录已删除", store.get(pdf) is None)
check("别的记录没被连坐", store.get(CORPUS / "甲.docx") is not None)
check("空输入返回 0 不报错", store.forget([]) == 0)
store.close()


# --------------------------------------------------------------------------- #
print("\n[10] --dry-run 不写任何文件（连带 --force 也不许清状态库）")
def snapshot():
    return {p: p.stat().st_mtime_ns for p in (BASE / "out").rglob("*") if p.is_file()}

before = snapshot()
store_before = StateStore(BASE / "state.db")
has_rec_before = store_before.get(pdf) is not None
store_before.close()

args5 = argparse.Namespace(config=str(CFGP), root=None, limit=0, quiet=True,
                           no_ocr=True, paths=[str(pdf)], list=None,
                           dry_run=True, force=True)
rc5, out5 = silent(cmd_convert, args5, load_config(CFGP))
check("退出码 0", rc5 == 0, str(rc5))
check("输出目录没有新增/改动", before == snapshot(),
      str(set(snapshot()) - set(before)))
store_after = StateStore(BASE / "state.db")
check("--dry-run 没有把状态记录清掉（不然后面真跑会变成全量重转）",
      has_rec_before and store_after.get(pdf) is not None)
store_after.close()
check("提示了未写文件", "未写入任何文件" in out5, out5[-200:])


# --------------------------------------------------------------------------- #
print("\n[11] 扩展名配置：缺键 / 写飘时不能变成「什么都不转」")
from doc2md.config import (                       # noqa: E402
    DEFAULT_WATCH_EXTENSIONS,
    normalize_extensions,
)

check("归一化：补点、小写、去空白、去重",
      normalize_extensions(["pdf", ".PDF", " docx ", "", ".xls", "xls"])
      == [".pdf", ".docx", ".xls"],
      str(normalize_extensions(["pdf", ".PDF", " docx ", "", ".xls", "xls"])))
check("非字符串项被丢掉（None 不该变成 .none）",
      normalize_extensions([None, 3, ["x"], ".pdf"]) == [".pdf"],
      str(normalize_extensions([None, 3, ["x"], ".pdf"])))
check("写成单个字符串也认", normalize_extensions(".pdf") == [".pdf"],
      str(normalize_extensions(".pdf")))

# 缺键 / 空列表都必须回退到默认全集。踩过：一份没写 watch_extensions 的最小配置
# 让所有 .docx 都被判成"格式不支持"，提示还指向了完全错误的方向。
for i, (label, data) in enumerate((("缺 watch_extensions", {}),
                                   ("watch_extensions 为空", {"watch_extensions": []}))):
    p = BASE / f"cfg_ext_{i}.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    loaded = load_config(p)
    check(f"{label} → 回退到默认全集",
          loaded.watch_extensions == list(DEFAULT_WATCH_EXTENSIONS),
          str(loaded.watch_extensions))


# --------------------------------------------------------------------------- #
shutil.rmtree(TMP, ignore_errors=True)
print("\n" + "=" * 74)
print(f"通过 {PASS}　失败 {len(FAILS)}")
if FAILS:
    for f in FAILS:
        print(f"  - {f}")
print("=" * 74)
sys.exit(1 if FAILS else 0)
