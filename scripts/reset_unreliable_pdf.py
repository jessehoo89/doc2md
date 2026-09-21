"""把"文本层不可信"的 PDF 从状态库里摘掉，让引擎重新走 OCR 识别。

背景：这些 PDF 之前被判为"文本型"，直接用文本层抽出了 md。但它们的文本层其实
是扫描件自带的 OCR 结果（或 CID 乱码 / 隐形搜索层），不可靠，必须改成重新 OCR。

用法：
  python reset_unreliable_pdf.py              # 只统计（dry-run）
  python reset_unreliable_pdf.py --apply      # 备份状态库并删除对应记录
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

# 仓库根 = doc2md 包的上一级（脚本位于 <root>/tests 或 <root>/scripts）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import doc2md.config as config  # noqa: E402
import doc2md.detect as detect  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--from-scan", action="store_true",
                    help="直接读 scan_result.json（省去重新探测，8 分钟）")
    args = ap.parse_args()

    cfg = config.load_config(ROOT / "config.json")
    print(f"文本层可信度检查: {cfg.pdf_trust_check}")
    print(f"OCR 路径: prefer_cloud={cfg.local_ocr.prefer_cloud}"
          f"（云端日上限 {cfg.ocr.daily_page_limit} 页）")
    print()

    srcs: list[Path] = []
    for root in cfg.roots:
        srcs.extend(sorted(Path(root).rglob("*.pdf")))
    print(f"源 PDF 共 {len(srcs)} 个", flush=True)

    t0 = time.time()
    bad: list[tuple[Path, int, str]] = []
    good = 0

    if args.from_scan:
        import json

        recs = json.loads((Path(__file__).with_name("scan_result.json")).read_text(encoding="utf-8"))
        root0 = Path(cfg.roots[0])
        for r in recs:
            if r["verdict"] in ("no_text", "garbled", "invisible_ocr", "image_backed"):
                bad.append((root0 / r["rel"], r.get("pages", 0), r["verdict"]))
            else:
                good += 1
        print(f"（读自 scan_result.json，共 {len(recs)} 条）")
    else:
        print("逐个判定文本层可信度…", flush=True)
        for i, p in enumerate(srcs, 1):
            try:
                ok, pages, why = detect.pdf_text_trust(
                    p, cfg.min_text_chars_per_page, cfg.text_pdf_probe_pages
                )
            except Exception as e:  # noqa: BLE001
                print(f"  [ERR] {p.name[:60]}: {e}")
                continue
            if ok:
                good += 1
            else:
                bad.append((p, pages, why))
            if i % 50 == 0:
                print(f"  {i}/{len(srcs)}  已用 {time.time()-t0:.0f}s  不可信 {len(bad)}", flush=True)

    kinds = Counter()
    for _p, _n, why in bad:
        key = why.split("（")[0]
        kinds[key] += 1
    print()
    print(f"判定结果：可信 {good}，不可信 {len(bad)}（{sum(n for _p, n, _w in bad)} 页）")
    for k, v in kinds.most_common():
        print(f"   {k:<22} {v:>5}")

    sensitive = [b for b in bad if config.is_sensitive(cfg, b[0])]
    print(f"\n其中命中敏感标记（会被强制本地 OCR）: {len(sensitive)}")
    for p, n, _w in sensitive[:10]:
        print(f"   {p.name[:70]}  ({n} 页)")

    if not bad:
        return 0

    db = Path(cfg.state_db)
    conn = sqlite3.connect(str(db))
    rows = {r[0].lower(): r for r in conn.execute("SELECT path, rowid, engine, status FROM files")}
    hits = []
    keep_cloud = []
    for p, n, why in bad:
        key = str(p).lower()
        r = rows.get(key)
        if not r:
            continue
        _path, rowid, engine, status = r
        # 已经是云端 VLM（engine='ocr'）且成功的，不必重做——它本来就不依赖文本层
        if engine == "ocr" and status == "ok":
            keep_cloud.append(p)
            continue
        hits.append((rowid, engine, status, p, why))
    print(f"\n状态库命中需重置的记录: {len(hits)} / {len(bad)}")
    eng = Counter(h[1] for h in hits)
    for k, v in eng.most_common():
        print(f"   engine={k:<12} {v}")
    print(f"已是云端 OCR 且成功、无需重做: {len(keep_cloud)}")
    missing = [b for b in bad if str(b[0]).lower() not in rows]
    print(f"状态库里没有记录（一定会被处理）: {len(missing)}")
    conn.close()

    if not args.apply:
        print("\n[dry-run] 未改动状态库。加 --apply 执行重置。")
        print(f"耗时 {time.time()-t0:.0f}s")
        return 0

    bak = db.with_name(db.name + f".bak-prereset-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(db, bak)
    print(f"\n已备份状态库 → {bak.name}")

    conn = sqlite3.connect(str(db))
    for rowid, _e, _s, _p, _w in hits:
        conn.execute("DELETE FROM files WHERE rowid = ?", (rowid,))
    conn.commit()
    n_left = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conn.close()
    print(f"已删除 {len(hits)} 条记录，状态库剩余 {n_left} 条")
    print("现在可以跑 `python -m doc2md run`，这些 PDF 会走 OCR 重新识别。")
    print(f"耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
