r"""清理 state.db 里「仅盘符大小写不同」的陈旧重复记录，以及它们多写出来的 md。

背景：state.db 里存在同一份源文件的成对记录，只差盘符大小写：
    f:\<语料根>\...\X.docx   md_path=...\X_ed7c5f.md
    F:\<语料根>\...\X.docx   md_path=...\X.md
`is_up_to_date()` 用 `WHERE path = ?` 精确匹配，小写那条永远不命中，
于是每次跑都会把小写记录当"新文件"重转一遍、多写一份 md。
（来源是已删除的一次性脚本用 `f:` 小写路径写过库；配置里始终是大写 `F:`，不会再产生。）

本脚本做三件事，默认全部只统计（dry-run）：
  1. 找出小写盘符记录，且**存在同源大写双胞胎**的 → 这些是纯冗余，可删；
  2. 这些小写记录指向的 md（通常是 `_xxxxxx.md` 哈希名），若**同源双胞胎的正式 md 也在**，
     则该哈希 md 是重复文件，可删；
  3. 其余孤立的哈希 md（无任何记录引用、但双胞胎 md 不存在）**不动**，交人工判断。

用法：
    python clean_stale_drivecase.py            # 只统计
    python clean_stale_drivecase.py --apply    # 备份 state.db 后执行
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

# 仓库根 = doc2md 包的上一级（脚本位于 <root>/tests 或 <root>/scripts）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import doc2md.config as config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cfg = config.load_config(ROOT / "config.json")
    db = Path(cfg.state_db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT path, size, status, engine, md_path, updated FROM files"
    ).fetchall()
    print(f"总记录: {len(rows)}")

    # 归一化（仅小写）分组
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        groups[r["path"].lower()].append(r)

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"归一化后重复的键: {len(dup_groups)}")

    # 只处理「盘符大小写」这一种差异，且必须恰好是两个记录
    stale: list[sqlite3.Row] = []
    keep: list[sqlite3.Row] = []
    skipped_other = 0
    for k, v in dup_groups.items():
        if len(v) != 2:
            skipped_other += 1
            continue
        a, b = v
        if a["path"] == b["path"]:
            skipped_other += 1
            continue
        # 差异必须只在盘符大小写
        if a["path"][2:].lower() != b["path"][2:].lower():
            skipped_other += 1
            continue
        # 小写盘符的算陈旧
        lower = a if a["path"][0].islower() else b
        upper = b if lower is a else a
        stale.append(lower)
        keep.append(upper)

    print(f"可删的陈旧小写记录: {len(stale)}")
    print(f"非盘符差异的重复组（不动）: {skipped_other}")

    if not stale:
        print("\n无需清理。")
        conn.close()
        return 0

    # md 引用计数（现存全部记录）
    ref_count: dict[str, int] = defaultdict(int)
    for r in rows:
        if r["md_path"]:
            ref_count[r["md_path"].lower()] += 1

    md_to_delete: list[Path] = []
    md_kept: list[Path] = []
    for s, u in zip(stale, keep):
        smd = Path(s["md_path"]) if s["md_path"] else None
        umd = Path(u["md_path"]) if u["md_path"] else None
        if smd and smd.exists():
            twin_ok = bool(umd and umd.exists())
            refs = ref_count.get(str(smd).lower(), 0)
            if twin_ok and refs <= 1:
                md_to_delete.append(smd)
            else:
                md_kept.append(smd)

    print(f"\n可删的重复 md: {len(md_to_delete)}"
          f"（合计 {sum(p.stat().st_size for p in md_to_delete)/1024:.0f} KB）")
    print(f"保留的哈希 md（双胞胎 md 缺失或有其他引用）: {len(md_kept)}")
    for p in md_to_delete[:6]:
        print(f"   删 {p.name[:78]}")
    for p in md_kept[:6]:
        print(f"   留 {p.name[:78]}")

    if not args.apply:
        print("\n[dry-run] 未改动。加 --apply 执行。")
        print("  执行前请确保 doc2md 没有在跑（会写 state.db）。")
        conn.close()
        return 0

    bak = db.with_name(db.name + f".bak-dedupe-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(db, bak)
    print(f"\n已备份状态库 → {bak.name}")

    for s in stale:
        conn.execute("DELETE FROM files WHERE path = ?", (s["path"],))
    conn.commit()
    left = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conn.close()
    print(f"已删除 {len(stale)} 条陈旧记录，状态库剩余 {left} 条")

    n_ok = n_fail = 0
    for p in md_to_delete:
        try:
            p.unlink()
            n_ok += 1
        except OSError as e:
            n_fail += 1
            print(f"   [ERR] 删不掉 {p.name}: {e}")
    print(f"已删除 {n_ok} 份重复 md，失败 {n_fail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
