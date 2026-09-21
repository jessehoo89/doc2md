# -*- coding: utf-8 -*-
"""MinerU 后端真实调用验证（会联网、会消耗 MinerU 额度）。

验证三件事：
  1. agent 模式（免 Token）能直接跑通本地 PDF，产出 Markdown；
  2. 产出的图片被下载并落盘到同名 .assets 目录，md 里的引用被改写成相对路径
     （引擎要求插图是本地文件）；
  3. 走 OcrRouter 的完整路径（这里只挂 mineru-agent 一个后端）能正常出 md。

用法：python test_mineru_live.py [pdf 路径]
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# 仓库根 = doc2md 包的上一级（脚本位于 <root>/tests 或 <root>/scripts）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOG_PATH = Path(__file__).with_name("test_mineru_live.log")

def _resolve_samples() -> list[Path]:
    """样例 PDF：命令行参数优先，其次环境变量 DOC2MD_MINERU_PDFS（os.pathsep 分隔），
    都没有就从 config.json 的 roots 里自动挑小扫描件。

    不写死路径的理由：本测试属于仓库，不该把某台机器的语料目录带进版本库。
    """
    env = os.environ.get("DOC2MD_MINERU_PDFS", "").strip()
    if env:
        return [Path(x) for x in env.split(os.pathsep) if x.strip()]
    try:
        from devkit import find_scanned_pdfs
    except ImportError:
        return []
    return find_scanned_pdfs(count=2, max_mb=10.0)


OUT_DIR = ROOT / "tmp" / "mineru_test"


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


def main() -> int:
    sys.stdout = _Tee(sys.stdout, LOG_PATH)
    sys.stderr = sys.stdout

    from doc2md import converters
    from doc2md.config import load_config
    from doc2md.mineru import MinerUOcrClient
    from doc2md.ocr_router import OcrRouter

    print("=" * 74)
    print("  MinerU 后端真实调用验证")
    print("=" * 74)

    cfg = load_config()
    agent_cfg = None
    for b in cfg.ocr_backends:
        if b.type == "mineru" and b.mode == "agent":
            agent_cfg = b
    if agent_cfg is None:
        print("[中止] 配置里找不到 mineru agent 后端")
        return 2

    print(f"agent 后端配置：{agent_cfg.name}  base_url={agent_cfg.base_url}  "
          f"max_pages={agent_cfg.max_pages}  max_file_mb={agent_cfg.max_file_mb}")
    prec = [b for b in cfg.ocr_backends if b.type == "mineru" and b.mode == "precision"]
    if prec:
        print(f"precision 后端：{prec[0].name}  token={'已配置' if prec[0].token else '未配置（自动跳过）'}")
    print()

    client = MinerUOcrClient(agent_cfg, store=None, verbose=True)
    ok, msg = client.ping()
    print(f"连通性自检：{'成功' if ok else '失败'} — {msg}\n")

    samples = [Path(p) for p in (sys.argv[1:] or DEFAULT_SAMPLES)]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0

    for src in samples:
        if not src.exists():
            print(f"[跳过] 文件不存在：{src}")
            continue
        size_mb = src.stat().st_size / 1048576.0
        print("-" * 74)
        print(f"文件：{src.name}")
        print(f"体积：{size_mb:.2f}MB")
        md_path = OUT_DIR / (src.stem + ".md")
        t0 = time.time()
        try:
            res = client.convert_file(src, md_path, pages=_guess_pages(src))
        except Exception as e:
            failures += 1
            print(f"[失败] {type(e).__name__}: {str(e)[:300]}")
            continue

        # 落盘（模拟引擎行为）
        md = converters.cleanup_markdown(converters.html_tables_to_markdown(res.markdown))
        converters.write_markdown(md_path, md, src.stem)

        assets = converters.assets_dir_for(md_path)
        files = sorted(assets.rglob("*")) if assets.exists() else []
        img_files = [f for f in files if f.is_file()]
        print(f"[成功] 后端={res.backend} 耗时={res.elapsed:.1f}s "
              f"报告页数={res.pages} 报告图片={res.image_count}")
        print(f"       md={md_path}")
        print(f"       字数={len(md)}  图片落盘={len(img_files)} 张 → {assets}")
        if img_files:
            for f in img_files[:5]:
                print(f"         · {f.relative_to(assets)}  {f.stat().st_size} B")
        print("       预览：")
        for line in md.splitlines()[:10]:
            print("         " + line[:100])
        # 检查残留远程引用
        remote = md.count("](http") + md.count('src="http')
        if remote:
            print(f"       [注意] md 里仍有 {remote} 处远程引用未本地化")
        print(f"       总耗时 {time.time() - t0:.1f}s")

    print()
    print("-" * 74)
    print("路由器路径验证（只挂 mineru-agent 一个后端）")
    only_agent = OcrRouter([(agent_cfg, client)], verbose=True, logger=print)
    src = samples[0]
    if src.exists():
        md_path = OUT_DIR / "_via_router.md"
        try:
            res = only_agent.convert_file(src, md_path, pages=_guess_pages(src))
            print(f"[成功] router 返回 backend={res.backend} attempts={res.attempts} "
                  f"chunks={res.chunks} 字数={len(res.markdown)}")
            print(f"       all_unavailable={only_agent.all_unavailable()}")
            print(f"       status={only_agent.status()[0]['state']}")
        except Exception as e:
            failures += 1
            print(f"[失败] {type(e).__name__}: {str(e)[:300]}")

    print()
    print("=" * 74)
    print(f"  完成，失败 {failures} 项")
    print(f"  日志：{LOG_PATH}")
    print("=" * 74)
    sys.stdout.flush()
    return 1 if failures else 0


def _guess_pages(src: Path) -> int:
    """借 detect 模块数一下页数；失败就按 1 页算。"""
    try:
        from doc2md import detect
        ok, pages, why = detect.pdf_text_trust(str(src))
        return pages or 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
