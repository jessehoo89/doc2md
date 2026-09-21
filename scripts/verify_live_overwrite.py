# -*- coding: utf-8 -*-
"""真机验证：强制重转一个真实源文件，确认①写回原来那个 md 名字 ②目录里不多出文件。

强制手段：删掉状态库里该文件的记录（等价于"从没转过"），不碰源文件本体。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 仓库根 = doc2md 包的上一级（脚本位于 <root>/tests 或 <root>/scripts）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ["DOC2MD_ENV_FILE"] = "none"

from doc2md.config import load_config      # noqa: E402
from doc2md.engine import Engine           # noqa: E402
from doc2md.state import StateStore        # noqa: E402

def _resolve_src() -> Path:
    """源文件：命令行第一个非选项参数优先；不传就从 config.json 的 roots 里自动
    挑一个小 docx —— 走本地引擎，不联网、不耗额度，几秒跑完。

    不写死路径的理由：本脚本属于仓库，不该把某台机器的语料目录带进版本库。
    """
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    if argv:
        p = Path(argv[0])
        if not p.exists():
            raise SystemExit(f"[ERR] 源文件不存在: {p}")
        return p
    from devkit import find_samples

    return find_samples([".docx"], count=1, max_mb=5, what="docx 源文件")[0]


LOG = Path(__file__).with_name("verify_live_overwrite.log")
lines: list[str] = []
w = lines.append


def main() -> None:
    src = _resolve_src()
    cfg = load_config(Path(ROOT) / "config.json")
    store = StateStore(cfg.state_db)
    eng = Engine(cfg, store, verbose=True, logger=w)

    w(f"源文件   : {src.name}")
    w(f"策略     : {cfg.output.on_collision}")
    w(f"覆盖旧md : {cfg.overwrite_existing_md}")
    w("")

    d = src.parent
    target = eng.md_path_for(src)
    out_dir = target.parent          # md 在输出镜像目录里，不在源目录旁边
    before = sorted(p.name for p in out_dir.glob("*.md"))
    w(f"输出目录 : {out_dir}")
    w(f"重转前该目录的 md（{len(before)} 个）：")
    for n in before:
        w(f"    {n}")
    w(f"\n解析出的输出路径：{target.name}")
    w(f"  该文件存在：{target.exists()}  "
      f"{'（' + str(len(target.read_text(encoding='utf-8-sig'))) + ' 字）' if target.exists() else ''}")
    mt_before = target.stat().st_mtime if target.exists() else 0

    # 强制重转：删掉状态库记录
    with store._lock:  # noqa: SLF001
        n = store._conn.execute("DELETE FROM files WHERE path = ?", (str(src),)).rowcount
        store._conn.commit()
    w(f"\n已删除状态库记录 {n} 条，开始重转…")

    t = eng.plan(src)
    status, info = eng.process(t)
    w(f"转换结果：{status}  ({info})")

    after = sorted(p.name for p in out_dir.glob("*.md"))
    mt_after = target.stat().st_mtime if target.exists() else 0
    w(f"\n重转后该目录的 md（{len(after)} 个）：")
    for n in after:
        w(f"    {n}")
    w("")
    w(f"解析路径是否就是原文件 : {target.name in before}")
    w(f"文件集合是否没变       : {set(after) == set(before)}")
    w(f"重转前后是否无新名字   : {not (set(after) - set(before))}")
    w(f"mtime 是否被刷新       : {mt_after >= mt_before}  ({mt_before:.0f} → {mt_after:.0f})")

    store.close()
    LOG.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n日志 -> {LOG}")


if __name__ == "__main__":
    main()
