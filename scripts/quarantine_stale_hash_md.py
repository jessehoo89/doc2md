r"""把输出目录里"陈旧重复"的哈希后缀 md（X_ab12cd.md）移入隔离目录。

背景：`Engine.md_path_for()` 在正规名 md 已被别人占用时，会给源文件另起一个
`<主名>_<sha1(源路径)[:6]>.md`。多轮重跑（换行符修复、文本层重判、OCR 重做）会让
同一份源、甚至同一批内容的旧产物残留下来，而状态库只认最新那条记录。
结果：磁盘上出现**没有任何状态库记录引用**的哈希 md，而它的同主名正规 md 更新、更完整。

判定（三条全满足才移，宁留不删）：
  1. 状态库 files 表**没有任何记录**的 md_path 指向它；
  2. 同主名正规 md（去掉 `_xxxxxx`）**存在**；
  3. 正规 md 的 mtime **不早于**该哈希 md（防止把"更新"的反过来清了）。

只**移动**到隔离目录（保留相对路径），不删除，可随时还原。

用法：
  python quarantine_stale_hash_md.py            # 只统计（dry-run）
  python quarantine_stale_hash_md.py --move     # 执行移动
  可选 --out <目录> 覆盖 config.json 的 output.root，
       --quarantine <目录> 覆盖默认隔离位置 <仓库根>\\_隔离\\陈旧MD_<日期>
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# 仓库根 = doc2md 包的上一级（脚本位于 <root>/tests 或 <root>/scripts）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import doc2md.config as config  # noqa: E402

HASH_RX = re.compile(r"_[0-9a-f]{6}\.md$", re.I)
# 在 main() 里解析；默认 <仓库根>/_隔离/陈旧MD_<日期>
QUARANTINE: Path | None = None


def main() -> int:
    global QUARANTINE
    ap = argparse.ArgumentParser()
    ap.add_argument("--move", action="store_true")
    ap.add_argument("--out", default=None, help="输出目录（默认取 config.output.root）")
    ap.add_argument("--quarantine", default=None,
                    help="隔离目录（默认 <仓库根>/_隔离/陈旧MD_<日期>）")
    args = ap.parse_args()

    QUARANTINE = (Path(args.quarantine) if args.quarantine
                  else ROOT / "_隔离" / f"陈旧MD_{time.strftime('%Y%m%d')}")

    cfg = config.load_config(ROOT / "config.json")
    out_dir = Path(args.out) if args.out else Path(cfg.output.root)
    if not out_dir.is_dir():
        print(f"[ERR] 输出目录不存在: {out_dir}")
        return 1

    conn = sqlite3.connect(str(cfg.state_db))
    refd = {
        os.path.normcase(r[0])
        for r in conn.execute("SELECT md_path FROM files WHERE md_path IS NOT NULL AND md_path <> ''")
    }
    conn.close()

    cands: list[tuple[Path, Path]] = []
    skipped_no_canon = skipped_older = skipped_refd = 0
    for p in out_dir.rglob("*.md"):
        if not HASH_RX.search(p.name):
            continue
        if os.path.normcase(str(p)) in refd:
            skipped_refd += 1
            continue
        canon = p.with_name(HASH_RX.sub(".md", p.name))
        if not canon.exists():
            skipped_no_canon += 1
            continue
        try:
            if canon.stat().st_mtime < p.stat().st_mtime - 1:
                skipped_older += 1
                continue
        except OSError:
            continue
        cands.append((p, canon))

    print(f"输出目录            : {out_dir}")
    print(f"哈希 md 被状态库引用 / 无正规 md / 正规 md 更旧  → 跳过: "
          f"{skipped_refd} / {skipped_no_canon} / {skipped_older}")
    print(f"可移入隔离的陈旧哈希 md: {len(cands)}")

    if not cands:
        print("\n无需处理。")
        return 0

    total = sum(p.stat().st_size for p, _ in cands)
    print(f"合计 {total / 1e6:.1f} MB")
    print("\n按目录分布（前 10）：")
    from collections import Counter
    c = Counter(str(p.parent.relative_to(out_dir)) for p, _ in cands)
    for k, v in c.most_common(10):
        print(f"  {v:>4}  {k[:95]}")
    print("\n样例：")
    for p, canon in cands[:8]:
        print(f"  {p.name[:66]}")
        print(f"      ← 保留 {canon.name[:70]}")

    if not args.move:
        print(f"\n[dry-run] 未移动。加 --move 执行 → {QUARANTINE}")
        return 0

    moved = failed = 0
    for p, _ in cands:
        dst = QUARANTINE / p.relative_to(out_dir)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                dst = dst.with_name(dst.stem + f"_{int(time.time() * 1000) % 100000}" + dst.suffix)
            shutil.move(str(p), str(dst))
            moved += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            if failed <= 5:
                print(f"  [失败] {p.name}: {e}")
    print(f"\n已移动 {moved} 份到隔离目录，失败 {failed}")
    print(f"隔离目录：{QUARANTINE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
