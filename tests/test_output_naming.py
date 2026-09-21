# -*- coding: utf-8 -*-
"""输出命名策略测试：同名不冲突、重转覆盖、绝不再出现 `_xxxxxx` 新名字。

背景：`x.docx` 与 `x.pdf`（同一份文档的两种格式）算出来的 md 路径都是 `x.md`。
旧逻辑在冲突时把后来者改名成 `x_<hash>.md`，于是同一份文档在输出目录里留下
两个名字，用户在监控模式下看到"改了文件就多出一个新文件"。

覆盖三种策略：
  overwrite —— 只有一个名字，重转覆盖（默认，本项目在用）
  stable    —— 先到先得 + 归属粘住，后到者另存 `x_<hash>.md`（重转仍同名）
  suffix    —— 旧行为（回归保护：确认仍能复现"多一个文件"）

用法：python test_output_naming.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# 仓库根 = doc2md 包的上一级（脚本位于 <root>/tests 或 <root>/scripts）
ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG = Path(ROOT) / "config.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ["DOC2MD_ENV_FILE"] = "none"   # 隔离真实凭据

LOG_PATH = Path(__file__).with_name("test_output_naming.log")

# 真实小样本：一个 docx、一个 xlsx，都走本地引擎（不触发云端 OCR，快且免费）。
# 从 config.json 的 roots 里自动挑，避免把个人语料路径写进仓库；
# 想固定样本就用环境变量 DOC2MD_SAMPLE_DOCX / DOC2MD_SAMPLE_XLSX 指定。
def _resolve_samples() -> tuple[Path | None, Path | None]:
    env_docx = os.environ.get("DOC2MD_SAMPLE_DOCX", "").strip()
    env_xlsx = os.environ.get("DOC2MD_SAMPLE_XLSX", "").strip()
    docx = Path(env_docx) if env_docx else None
    xlsx = Path(env_xlsx) if env_xlsx else None
    if docx and xlsx:
        return docx, xlsx
    try:
        from devkit import find_samples
    except ImportError:
        return docx, xlsx
    for ext, holder in ((".docx", "docx"), (".xlsx", "xlsx")):
        if (docx if holder == "docx" else xlsx):
            continue
        try:
            got = find_samples([ext], count=1, max_mb=5, what=f"{ext} 样本")[0]
        except FileNotFoundError:
            continue
        if holder == "docx":
            docx = got
        else:
            xlsx = got
    return docx, xlsx


SAMPLE_DOCX, SAMPLE_XLSX = _resolve_samples()

FAILURES = 0


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


def build_cfg(tmp: Path, on_collision: str):
    from doc2md.config import load_config

    data = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
    (tmp / "src").mkdir(parents=True, exist_ok=True)
    data["roots"] = [str(tmp / "src")]
    data["output"] = {
        "mode": "custom",
        "root": str(tmp / "out"),
        "layout": "mirror",
        "on_collision": on_collision,
    }
    data["state_db"] = str(tmp / "state.db")
    data["log_dir"] = str(tmp / "logs")
    data["overwrite_existing_md"] = "overwrite"
    data["state_db"] = str(tmp / "state.db")
    ocr = data.setdefault("ocr", {})
    ocr["enabled"] = False          # 本测试只走本地引擎
    ocr["backends"] = []
    data["local_ocr"] = dict(data.get("local_ocr") or {})
    data["local_ocr"]["enabled"] = False
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_config(cfg_path)


def t_default(tmp: Path) -> None:
    """不写 on_collision 时的兜底值：必须是 stable（安全项），而不是会把
    同名双胞胎内容覆盖掉的 overwrite。"""
    from doc2md.config import OutputConfig

    print("\n[0] 默认策略兜底")
    check("OutputConfig 默认 stable", OutputConfig().on_collision == "stable",
          OutputConfig().on_collision)
    cfg = build_cfg(tmp, "stable")
    check("config.json 里也写着 stable", cfg.output.on_collision == "stable",
          cfg.output.on_collision)
    # 写个非法值，应被兜回 stable 而不是崩溃
    import json as _json
    data = _json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
    data["output"] = {"mode": "custom", "root": str(tmp / "out"), "on_collision": "乱写"}
    p = tmp / "bad.json"
    p.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
    from doc2md.config import load_config
    check("非法取值兜回 stable", load_config(p).output.on_collision == "stable",
          load_config(p).output.on_collision)


def hash_of(path: Path) -> str:
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:6]


# ---------------- 1. 路径解析 ----------------

def t_paths_overwrite(tmp: Path) -> None:
    from doc2md.engine import Engine
    from doc2md.state import StateStore

    print("\n[1] overwrite：同名不同源 → 同一个 md 路径，重转不变")
    cfg = build_cfg(tmp, "overwrite")
    src = tmp / "src"
    d = src / "x.docx"
    p = src / "x.pdf"
    d.write_bytes(b"d")
    p.write_bytes(b"p")
    want = tmp / "out" / "x.md"

    store = StateStore(cfg.state_db)
    eng = Engine(cfg, store, verbose=False)
    a = eng.md_path_for(d)
    b = eng.md_path_for(p)
    check("docx 与 pdf 都指向 x.md", a == want and b == want, f"{a} / {b}")

    # 记成已完成，再算一遍（模拟重转）
    store.mark_ok(d, 1, 1.0, "docx", str(a), 1)
    store.mark_ok(p, 1, 1.0, "pdf-text", str(b), 1)
    check("已转换后再算仍同名", eng.md_path_for(d) == want and eng.md_path_for(p) == want)

    # 换一个 Engine 实例（模拟监控模式重启），顺序反转，名字必须还一样
    eng2 = Engine(cfg, store, verbose=False)
    check("重启 + 顺序反转后仍同名",
          eng2.md_path_for(p) == want and eng2.md_path_for(d) == want,
          f"{eng2.md_path_for(p)} / {eng2.md_path_for(d)}")
    store.close()


def t_paths_stable(tmp: Path) -> None:
    from doc2md.engine import Engine
    from doc2md.state import StateStore

    print("\n[2] stable：先到先得 + 归属粘住，跨进程同名")
    cfg = build_cfg(tmp, "stable")
    src = tmp / "src"
    d = src / "x.docx"
    p = src / "x.pdf"
    d.write_bytes(b"d")
    p.write_bytes(b"p")
    base = tmp / "out" / "x.md"
    d_hash = tmp / "out" / f"x_{hash_of(d)}.md"

    store = StateStore(cfg.state_db)
    eng = Engine(cfg, store, verbose=False)
    first = eng.md_path_for(p)
    check("先到的拿到 x.md", first == base, str(first))
    store.mark_ok(p, 1, 1.0, "pdf-text", str(first), 1)

    second = eng.md_path_for(d)
    check("后到的另存 x_<源路径哈希>.md", second == d_hash, str(second))
    store.mark_ok(d, 1, 1.0, "docx", str(second), 1)

    # 重启 + 顺序反转：两份都必须还是原名（这是旧逻辑会漂移的地方）
    eng2 = Engine(cfg, store, verbose=False)
    check("重启后后到者仍叫 x_<hash>.md（不漂移）", eng2.md_path_for(d) == d_hash,
          str(eng2.md_path_for(d)))
    check("重启后先到者仍是 x.md", eng2.md_path_for(p) == base, str(eng2.md_path_for(p)))

    # 归属被清成非 ok（模拟上一轮失败）时，也不该把别人的名字抢走
    with store._lock:  # noqa: SLF001
        store._conn.execute("UPDATE files SET status='failed' WHERE path=?", (str(d),))
        store._conn.commit()
    eng3 = Engine(cfg, store, verbose=False)
    check("归属记录非 ok 时仍沿用原归属（不抢 x.md）", eng3.md_path_for(d) == d_hash,
          str(eng3.md_path_for(d)))
    store.close()


def t_paths_suffix(tmp: Path) -> None:
    from doc2md.engine import Engine
    from doc2md.state import StateStore

    print("\n[3] suffix：旧行为回归保护 —— 确认仍会多出一个文件")
    cfg = build_cfg(tmp, "suffix")
    src = tmp / "src"
    d = src / "x.docx"
    p = src / "x.pdf"
    d.write_bytes(b"d")
    p.write_bytes(b"p")

    store = StateStore(cfg.state_db)
    eng = Engine(cfg, store, verbose=False)
    a = eng.md_path_for(p)
    store.mark_ok(p, 1, 1.0, "pdf-text", str(a), 1)
    b = eng.md_path_for(d)
    check("suffix 下后到者仍被改名", a != b and b.stem.startswith("x_"), f"{a} / {b}")
    store.close()


def t_path_case(tmp: Path) -> None:
    from doc2md.engine import _same_path

    print("\n[4] 路径比较对盘符大小写/分隔符不敏感")
    check("F:\\ 与 f:\\ 视为同一路径",
          _same_path(Path(r"F:\a\b\c.md"), Path(r"f:\a\b\c.md")))
    check("斜杠方向不影响判断",
          _same_path(Path("F:/a/b/c.md"), Path(r"F:\a\b\c.md")))
    check("不同文件名仍能区分",
          not _same_path(Path(r"F:\a\b\c.md"), Path(r"F:\a\b\d.md")))


# ---------------- 5. 覆盖写入与旧图清理 ----------------

def t_overwrite_write(tmp: Path) -> None:
    from doc2md.config import load_config
    from doc2md.engine import Engine
    from doc2md.state import StateStore

    print("\n[5] 覆盖旧 md：内容被替换，僵尸插图被清掉")
    cfg = build_cfg(tmp, "overwrite")
    out = tmp / "out"
    md = out / "x.md"
    assets = out / "x.assets"
    assets.mkdir(parents=True)
    (assets / "img1.png").write_bytes(b"1")
    (assets / "img2.png").write_bytes(b"2")
    (assets / "img3.png").write_bytes(b"3")
    md.write_text("旧内容 " + "x" * 500, encoding="utf-8-sig")

    store = StateStore(cfg.state_db)
    eng = Engine(cfg, store, verbose=False)
    eng._write_md(md, "![图](x.assets/img1.png)\n\n新内容", None)  # noqa: SLF001

    text = md.read_text(encoding="utf-8-sig")
    check("md 被覆盖为新内容", "新内容" in text and "旧内容" not in text)
    check("仍被引用的图保留", (assets / "img1.png").exists())
    check("不再引用的图被清掉", not (assets / "img2.png").exists()
          and not (assets / "img3.png").exists())

    # 再覆盖一次：新内容完全不引用插图 → 目录被清空并删除
    eng._write_md(md, "纯文字，没有任何插图", None)  # noqa: SLF001
    check("新内容无插图时 assets 目录被清空移除",
          not assets.exists(), f"still: {list(assets.iterdir()) if assets.exists() else 'gone'}")
    store.close()


# ---------------- 6. 端到端：真实文件同名冲突 ----------------

def t_end_to_end(tmp: Path, on_collision: str, expect_files: int) -> None:
    from doc2md.config import load_config
    from doc2md.engine import Engine
    from doc2md.state import StateStore

    print(f"\n[6] 端到端（on_collision={on_collision}）：同名 docx + xlsx 实际转换")
    cfg = build_cfg(tmp, on_collision)
    src = tmp / "src"
    a = src / "同名冲突.docx"
    b = src / "同名冲突.xlsx"
    shutil.copy2(SAMPLE_DOCX, a)
    shutil.copy2(SAMPLE_XLSX, b)

    store = StateStore(cfg.state_db)
    eng = Engine(cfg, store, verbose=True, logger=print)
    rep = eng.run(dry_run=False, limit=0)
    print(f"    第一轮：ok={rep.ok} fail={rep.failed} 引擎={dict(rep.by_engine)}")

    out = tmp / "out"
    mds = sorted(out.rglob("*.md"))
    names = [m.name for m in mds]
    check(f"产出 {expect_files} 个 md，实得 {len(mds)}：{names}", len(mds) == expect_files)
    check("主名字是 同名冲突.md", (out / "同名冲突.md") in mds, str(names))
    if on_collision == "overwrite":
        check("overwrite 下没有带哈希后缀的名字",
              all(n == "同名冲突.md" for n in names), str(names))

    # 第二轮：改动源文件逼重转。硬性要求：文件集合与第一轮**完全一致**，
    # 不许冒出任何新名字（这是"重转不改名"的直接表述，两种模式都成立）。
    time.sleep(1.1)
    os.utime(a, None)
    eng.run(dry_run=False, limit=0)
    mds2 = sorted(out.rglob("*.md"))
    names2 = [m.name for m in mds2]
    check(f"重转后文件集合不变，第一轮 {names} / 第二轮 {names2}", set(names2) == set(names))
    for m in mds2:
        n = len(m.read_text(encoding="utf-8-sig"))
        check(f"{m.name} 有实质内容（{n} 字）", n > 50)
    eng.close()
    store.close()


def main() -> int:
    sys.stdout = _Tee(sys.stdout, LOG_PATH)
    sys.stderr = sys.stdout

    print("=" * 74)
    print("  输出命名策略测试：同名覆盖、重转不改名")
    print("=" * 74)

    if not (SAMPLE_DOCX and SAMPLE_XLSX
            and SAMPLE_DOCX.exists() and SAMPLE_XLSX.exists()):
        print(f"[跳过] 没找到可用的 docx / xlsx 样本：{SAMPLE_DOCX} / {SAMPLE_XLSX}")
        print("       请把 config.json 的 roots 指向真实语料目录，")
        print("       或用 DOC2MD_SAMPLE_DOCX / DOC2MD_SAMPLE_XLSX 指定样本。")
        return 2

    with tempfile.TemporaryDirectory(prefix="doc2md_name_", ignore_cleanup_errors=True) as d:
        base = Path(d)
        t_default(base / "c0")
        t_paths_overwrite(base / "c1")
        t_paths_stable(base / "c2")
        t_paths_suffix(base / "c3")
        t_path_case(base / "c4")
        t_overwrite_write(base / "c5")
        # overwrite：只有一个名字
        t_end_to_end(base / "c6", "overwrite", 1)
        # suffix：旧行为确实会多出一个文件（证明上面的断言有效）
        t_end_to_end(base / "c7", "suffix", 2)

    print()
    print("=" * 74)
    print(f"  失败项：{FAILURES}")
    print(f"  日志：{LOG_PATH}")
    print("=" * 74)
    sys.stdout.flush()
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
