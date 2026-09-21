"""清理输出目录里"没有任何 Markdown 引用"的孤立插图。

背景：doc2md 会把文档内嵌图片抽到 <同名>.assets/ 目录。同一份文档被重跑多轮时，
每轮都会重新提图并新建一套资源目录（xxx.assets、xxx_fbc718.assets …），
于是产生大量无人引用的图片副本。

判定原则（保守，宁留不删）：
  1. 图片若被同目录 / 上级目录 / 上上级目录里任何 .md 引用了文件名，即视为"在用"；
  2. 只处理扩展名为 jpg/jpeg/png/wmf/emf 的资源图片；
  3. 不删除，一律**移动到隔离目录**（保留相对路径），确认无误后由用户自行清空。

用法：
  python clean_orphan_images.py                    # 只统计，不动文件（dry-run）
  python clean_orphan_images.py --move             # 移动到隔离目录
  python clean_orphan_images.py --out D:\\md --move  # 显式指定要清理的目录
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from pathlib import Path

# 仓库根 = doc2md 包的上一级（本脚本位于 <root>/scripts）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from devkit import load_cfg  # noqa: E402

# 这两个在 main() 里按 config.json / 命令行解析后赋值
OUT: Path | None = None
QUARANTINE: Path | None = None
IMG_EXTS = {".jpg", ".jpeg", ".png", ".wmf", ".emf"}

# Markdown 图片语法 ![](...) / <img src="..."> / 裸文件名
NAME_RX = re.compile(r"([^\s\"'()\[\]<>|\\/]+\.(?:jpe?g|png|wmf|emf))", re.I)
# 带目录的引用（用于建"资源目录/文件名"片段索引，兜住远处目录的反向引用）
PATH_RX = re.compile(r"([^\s\"'()\[\]<>|]+\.(?:jpe?g|png|wmf|emf))", re.I)


def mentioned_names(md_path: Path) -> tuple[set[str], set[str]]:
    """返回 (提及的文件名集合, 提及的"目录/文件名"末两段片段集合)。"""
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set(), set()
    names: set[str] = set()
    frags: set[str] = set()
    for m in NAME_RX.finditer(text):
        names.add(m.group(1).lower())
    for m in PATH_RX.finditer(text):
        tok = m.group(1).lower().replace("\\", "/")
        parts = [x for x in tok.split("/") if x and x not in (".", "..")]
        if len(parts) >= 2:
            frags.add("/".join(parts[-2:]))
    return names, frags


def main() -> int:
    global OUT, QUARANTINE
    ap = argparse.ArgumentParser()
    ap.add_argument("--move", action="store_true", help="执行移动（默认只统计）")
    ap.add_argument("--out", default=None,
                    help="要清理的输出目录（默认取 config.json 的 output.root）")
    ap.add_argument("--quarantine", default=None,
                    help="隔离目录（默认 <仓库根>/_隔离/孤立图片_<日期>）")
    args = ap.parse_args()

    cfg = load_cfg()
    if args.out:
        OUT = Path(args.out)
    elif cfg.output.is_custom and str(cfg.output.root).strip():
        OUT = Path(cfg.output.root)
    else:
        print("[ERR] config.json 里没配 output.root（当前是 alongside 模式）")
        print("      请用 --out 指定要清理的目录，或先把 output 配成 custom。")
        return 1
    QUARANTINE = (Path(args.quarantine) if args.quarantine
                  else ROOT / "_隔离" / f"孤立图片_{time.strftime('%Y%m%d')}")

    if not OUT.is_dir():
        print(f"[ERR] 输出目录不存在: {OUT}")
        return 1

    t0 = time.time()
    # 1) 收集每个 md 提及的图片文件名，按目录归档；同时建一份"全库路径片段"索引，
    #    用来兜住"远处目录里的 md 反向引用本目录资源"这种少见情况。
    md_mentions: dict[str, set[str]] = {}
    global_frags: set[str] = set()
    md_count = 0
    for p in OUT.rglob("*.md"):
        md_count += 1
        names, frags = mentioned_names(p)
        md_mentions.setdefault(os.path.normcase(str(p.parent)), set()).update(names)
        global_frags |= frags
    print(f"扫描 {md_count} 个 md，覆盖 {len(md_mentions)} 个目录，"
          f"收集路径片段 {len(global_frags)} 个")

    # 2) 逐张图片判断
    orphans: list[Path] = []
    kept = 0
    saved_by_frag = 0
    total = 0
    for p in OUT.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in IMG_EXTS:
            continue
        total += 1
        name = p.name.lower()
        # 自身目录 + 两级祖先目录的 md 都算"可能引用者"
        d = p.parent
        cand_dirs = [d]
        if d.parent != d:
            cand_dirs.append(d.parent)
        if d.parent.parent != d.parent:
            cand_dirs.append(d.parent.parent)
        if any(name in md_mentions.get(os.path.normcase(str(c)), ()) for c in cand_dirs):
            kept += 1
            continue
        # 兜底：全库是否存在 "资源目录名/文件名" 形式的引用
        frag = f"{d.name.lower()}/{name}"
        if frag in global_frags:
            kept += 1
            saved_by_frag += 1
            continue
        orphans.append(p)

    print(f"图片总数 {total}；被引用保留 {kept}（其中靠全局片段兜回 {saved_by_frag}）；"
          f"判定孤立 {len(orphans)}")
    size = 0
    for p in orphans:
        try:
            size += p.stat().st_size
        except OSError:
            pass
    print(f"孤立图片合计 {size / 1e6:.0f} MB")

    # 按目录汇总，便于核对
    from collections import Counter

    c = Counter()
    for p in orphans:
        rel = p.relative_to(OUT)
        parts = rel.parts
        c[os.sep.join(parts[:2]) if len(parts) >= 2 else rel.parts[0]] += 1
    print("\n孤立图片最集中的 12 个位置：")
    for k, v in c.most_common(12):
        print(f"  {v:>5}  {k[:95]}")

    if not orphans:
        print("\n没有孤立图片，无需处理。")
        return 0

    if not args.move:
        print(f"\n[dry-run] 未移动任何文件。加 --move 执行移动 → {QUARANTINE}")
        print(f"耗时 {time.time() - t0:.1f}s")
        return 0

    # 3) 移动到隔离目录
    moved = 0
    failed = 0
    for p in orphans:
        rel = p.relative_to(OUT)
        dst = QUARANTINE / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                dst = dst.with_name(dst.stem + f"_{int(time.time() * 1000) % 100000}" + dst.suffix)
            shutil.move(str(p), str(dst))
            moved += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            if failed <= 5:
                print(f"  [失败] {rel}: {e}")
    print(f"\n已移动 {moved} 张到隔离目录，失败 {failed} 张")
    print(f"隔离目录：{QUARANTINE}")
    print(f"耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
