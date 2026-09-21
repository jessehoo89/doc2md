# -*- coding: utf-8 -*-
"""清理「无主历史残留 md」—— 旧命名漂移逻辑留下的重复旧版本。

判定（两条硬守卫，全部满足才算残留）：
  ① **状态库里没有任何记录指向它**（`lower(md_path)` 比对）→ 以后永远不会再被写入；
  ② **同主名的另一份 md 存在、且它才是状态库认定的权威产出** → 删掉不丢内容。

**为什么不用 mtime 判谁是权威版**：本库实测过反例 —— `"9·29"重大火灾事故调查报告_cb6c45.md`
的 mtime 比正式版新 3 小时，但它的内容只有 1 个标题、97 个空行变 12 个（**还是未做段落重组的
硬换行版**），是旧代码 + 已删除的"幽灵小写盘符记录"跑出来的产物。
**写得晚 ≠ 质量好**，所以权威版一律以状态库的引用为准，mtime 只作为提示打印。

默认 dry-run，只打印；加 `--apply` 才真删。
用法：python cleanup_orphan_md.py [--apply]
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

# 仓库根 = doc2md 包的上一级（脚本位于 <root>/tests 或 <root>/scripts）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ["DOC2MD_ENV_FILE"] = "none"

from doc2md.config import load_config      # noqa: E402

LOG = Path(__file__).with_name("cleanup_orphan_md.log")
HASH_RE = re.compile(r"^(?P<base>.+)_(?P<h>[0-9a-f]{6})$", re.I)

lines: list[str] = []
w = lines.append


def main() -> int:
    apply = "--apply" in sys.argv
    cfg = load_config(Path(ROOT) / "config.json")
    out_root = Path(cfg.output.root)

    conn = sqlite3.connect(str(cfg.state_db))
    conn.row_factory = sqlite3.Row
    referenced = {
        str(r["md_path"]).lower()
        for r in conn.execute("SELECT md_path FROM files WHERE md_path IS NOT NULL AND md_path <> ''")
    }
    conn.close()

    w(f"输出根   : {out_root}")
    w(f"状态库引用: {len(referenced)} 个 md 路径")
    w(f"模式     : {'APPLY（真删）' if apply else 'DRY-RUN（只看不动）'}")
    w("")

    all_md = [p for p in out_root.rglob("*.md")]
    targets: list[tuple[Path, Path, str]] = []   # (要删的, 权威版, 类型)
    skipped: list[tuple[Path, str]] = []

    for p in all_md:
        key = str(p).lower()
        if key in referenced:
            continue                      # 有主，不动
        m = HASH_RE.match(p.stem)
        if m:
            # 哈希名字：权威版是去后缀的那个
            sib = [p.with_name(m.group("base") + ".md")]
            kind = "哈希孤儿"
        else:
            # 平原名字：候选权威版是同目录下同主名的 `x_??????.md`
            sib = [q for q in p.parent.glob("*.md")
                   if (mm := HASH_RE.match(q.stem)) and mm.group("base") == p.stem]
            kind = "平原孤儿"

        if not sib:
            skipped.append((p, "找不到同主名的兄弟文件（唯一副本）"))
            continue
        live = [q for q in sib if q.exists() and str(q).lower() in referenced]
        if not live:
            skipped.append((p, "两份都无状态库引用，无法判断谁是权威版"))
            continue
        best = live[0]
        targets.append((p, best, kind))

    w(f"判定为残留：{len(targets)} 个")
    total = 0
    for p, keep, kind in targets:
        size = p.stat().st_size
        total += size
        warn = ""
        if p.stat().st_mtime > keep.stat().st_mtime:
            warn = "   ⚠ 它的 mtime 反而更新（旧代码产物，内容不一定更好，人工留意）"
        w(f"  [{kind}] {p}")
        w(f"            权威版 -> {keep.name}{warn}")
    w(f"合计 {total / 1024:.1f} KB")
    w("")
    w(f"跳过（有主/守卫拦下）：{len(skipped)} 个")
    for p, why in skipped[:60]:
        w(f"  - {p.name}  ← {why}")
    if len(skipped) > 60:
        w(f"  ... 另有 {len(skipped) - 60} 个")

    if apply:
        w("")
        w("开始删除…")
        ok = bad = 0
        for p, _keep, _kind in targets:
            try:
                os.remove(p)
                ok += 1
            except OSError as e:
                bad += 1
                w(f"  [失败] {p.name}: {e}")
        w(f"已删除 {ok} 个，失败 {bad} 个。")

    LOG.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n日志 -> {LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
