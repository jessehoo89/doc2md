"""试跑云端 PaddleOCR-VL：测速 + 与现有（文本层抽取的）md 对比质量。

用法：
  python test_cloud_ocr.py "<pdf相对路径>" [--pages N]
输出写到 scripts/_cloudtest/ 下，不动正式产物。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# 仓库根 = doc2md 包的上一级（脚本位于 <root>/tests 或 <root>/scripts）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

import doc2md.config as config  # noqa: E402
import doc2md.ocr as ocr  # noqa: E402

# 源目录取 config.json 的第一个 root；旧 md 所在目录取 output.root
# （alongside 模式就是源目录旁边）。都不写死 —— 本测试属于仓库，
# 不该把某台机器的语料路径带进版本库。
_cfg0 = config.load_config(ROOT / "config.json")
SRC = Path(_cfg0.roots[0]) if _cfg0.roots else ROOT
OUT = Path(_cfg0.output.root) if _cfg0.output.is_custom else SRC
TMP = ROOT / "tmp" / "cloudtest"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rel", help="源 PDF：相对 SRC 的路径，或一个绝对路径")
    ap.add_argument("--pages", type=int, default=0, help="只提交前 N 页（0=整份）")
    args = ap.parse_args()

    pdf = Path(args.rel)
    if not pdf.is_absolute():
        pdf = SRC / pdf
    if not pdf.exists():
        print("找不到:", pdf)
        return 1
    pymupdf.TOOLS.mupdf_display_errors(False)
    doc = pymupdf.open(str(pdf))
    total = doc.page_count
    doc.close()

    cfg = config.load_config(ROOT / "config.json")
    TMP.mkdir(parents=True, exist_ok=True)
    md_path = TMP / (pdf.stem + ".md")
    if md_path.exists():
        md_path.unlink()

    pages = args.pages or total
    client = ocr.PaddleOcrClient(cfg, store=None, verbose=True)
    print(f"提交: {pdf.name}  {pages}/{total} 页")
    t0 = time.time()
    try:
        res = client.convert_file(pdf, md_path, pages=pages)
    except Exception as e:  # noqa: BLE001
        print(f"[失败] {type(e).__name__}: {e}")
        return 2
    dt = time.time() - t0
    md_path.write_text(res.markdown, encoding="utf-8")
    print(f"\n完成: {res.pages} 页 / {dt:.1f}s = {dt/max(res.pages,1):.2f} s/页, "
          f"图片 {res.image_count} 张, 输出 {os.path.getsize(md_path)/1024:.0f} KB")

    new = md_path.read_text(encoding="utf-8", errors="replace")
    if Path(args.rel).is_absolute():
        oldp = pdf.with_suffix(".md")
    else:
        oldp = OUT / (args.rel[:-4] + ".md")
    print()
    print("=== 新（云端 VLM）前 500 字 ===")
    print(new[:500].replace("\n", " | "))
    if oldp.exists():
        old = oldp.read_text(encoding="utf-8", errors="replace")
        print()
        print(f"=== 旧（文本层抽取）{os.path.getsize(oldp)/1024:.0f} KB / 新 {os.path.getsize(md_path)/1024:.0f} KB ===")
        print(old[:500].replace("\n", " | "))
        print()
        print(f"旧的行数 {old.count(chr(10))}  新的行数 {new.count(chr(10))}")
        print(f"旧含表格标记 '|' 行数 {sum(1 for l in old.split(chr(10)) if l.count('|') >= 3)}"
              f"  新 {sum(1 for l in new.split(chr(10)) if l.count('|') >= 3)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
