# -*- coding: utf-8 -*-
"""全库回归：用真实 state.db 的副本，验证 3797 个源文件在新逻辑下的输出路径
与状态库里已记录的完全一致 —— 也就是"下一次运行不会有任何文件被改名/搬走"。

只读：状态库先复制到临时目录再打开。
用法：python _verify_stable_names.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# 仓库根 = doc2md 包的上一级（脚本位于 <root>/tests 或 <root>/scripts）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ["DOC2MD_ENV_FILE"] = "none"

from doc2md.config import load_config          # noqa: E402
from doc2md.engine import Engine, _norm        # noqa: E402
from doc2md.state import StateStore            # noqa: E402

LOG = Path(__file__).with_name("verify_output_names.log")
REAL_DB = Path(ROOT) / "state.db"
lines: list[str] = []
w = lines.append


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="doc2md_verify_"))
    db_copy = tmp / "state.db"
    shutil.copy2(REAL_DB, db_copy)
    for ext in ("-wal", "-shm"):
        p = Path(str(REAL_DB) + ext)
        if p.exists():
            shutil.copy2(p, Path(str(db_copy) + ext))

    cfg = load_config(Path(ROOT) / "config.json")
    cfg.state_db = db_copy
    w(f"输出根     : {cfg.output.root}")
    w(f"重名策略   : {cfg.output.on_collision}")
    w(f"状态库副本 : {db_copy}")
    w("")

    conn = sqlite3.connect(str(db_copy))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT path, md_path, status FROM files").fetchall()
    conn.close()

    store = StateStore(db_copy)
    eng = Engine(cfg, store, verbose=False)

    changed, same, no_md, missing = [], 0, 0, 0
    for r in rows:
        src = Path(r["path"])
        recorded = r["md_path"] or ""
        if not src.exists():
            missing += 1
        got = str(eng.md_path_for(src))
        if not recorded:
            no_md += 1
            if _norm(got) != _norm(src.with_suffix(".md")) and not got.lower().endswith(".md"):
                pass
            continue
        if _norm(got) == _norm(recorded):
            same += 1
        else:
            changed.append((r["status"], src, recorded, got))

    w(f"总计 {len(rows)} 条：")
    w(f"  路径不变        {same}")
    w(f"  无 md_path 记录 {no_md}")
    w(f"  源文件已不存在  {missing}")
    w(f"  路径会变        {len(changed)}")
    w("")
    if changed:
        w("以下条目路径会变（需要人工确认）：")
        for st, src, rec, got in changed:
            w(f"  [{st}] {src.name}")
            w(f"        原: {rec}")
            w(f"        新: {got}")
    else:
        w("结论：下一次运行不会搬动任何一个输出文件。")

    store.close()
    LOG.write_text("\n".join(lines), encoding="utf-8")
    print("done ->", LOG)
    print("\n".join(lines[:8]))
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
