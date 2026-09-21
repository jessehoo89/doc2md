"""开发辅助：定位仓库根、从语料里自动挑样例文件。

**doc2md 包本身不 import 本模块** —— 它只服务于 `tests/` 与 `scripts/` 里的
开发和运维脚本，因此放在仓库根，由那些脚本通过 sys.path 引入。

存在的理由：脚本里**不写死任何个人路径**。需要的样例一律从 config.json 的
`roots` 里按条件挑选，于是仓库可以安全地公开、分享、换台机器跑。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterable, Iterator

ROOT = Path(__file__).resolve().parent


def load_cfg():
    """加载仓库根的 config.json（缺失时会自动从 config.example.json 生成）。"""
    from doc2md.config import load_config

    return load_config(ROOT / "config.json")


def iter_candidates(
    exts: Iterable[str],
    *,
    roots: Iterable[str | Path] | None = None,
    max_mb: float | None = None,
) -> Iterator[tuple[Path, int]]:
    """按稳定顺序遍历候选文件，产出 (路径, 字节数)。

    未显式给 roots 时用 config.json 的 roots，并且会跳过排除目录
    （.git / __pycache__ / 缓存文件 等），避免把生成物当成样本来跑。
    """
    cfg = None
    if roots is None:
        cfg = load_cfg()
        roots = cfg.roots

    want = {e.lower() for e in exts}
    seen: set[str] = set()

    for root in roots:
        rp = Path(root)
        if not rp.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(rp):
            if cfg is not None:
                from doc2md.config import is_excluded

                dirnames[:] = [
                    d for d in sorted(dirnames)
                    if not is_excluded(cfg, Path(dirpath) / d)
                ]
            else:
                dirnames[:] = sorted(dirnames)
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                if p.suffix.lower() not in want:
                    continue
                key = str(p).lower()
                if key in seen:
                    continue
                seen.add(key)
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                if size == 0:
                    continue
                if max_mb is not None and size > max_mb * 1048576:
                    continue
                yield p, size


def find_samples(
    exts: Iterable[str],
    *,
    count: int = 1,
    roots: Iterable[str | Path] | None = None,
    max_mb: float | None = None,
    predicate: Callable[[Path], bool] | None = None,
    what: str = "",
) -> list[Path]:
    """挑出 count 个样例文件；挑不到就抛 FileNotFoundError（并说明怎么修）。

    predicate 用于加自定义筛选（例如"必须是扫描件"，见 find_scanned_pdfs）。
    """
    label = what or "/".join(sorted(exts))
    picked: list[Path] = []
    for p, _size in iter_candidates(exts, roots=roots, max_mb=max_mb):
        if predicate is not None:
            try:
                if not predicate(p):
                    continue
            except Exception:
                # 单个文件探不动就跳过，别让一个坏样本毁掉整轮挑选
                continue
        picked.append(p)
        if len(picked) >= count:
            return picked

    roots_txt = "、".join(str(r) for r in (roots or load_cfg().roots)) or "(空)"
    raise FileNotFoundError(
        f"没找到可用的{label}样例。检查两处：\n"
        f"  1. config.json 的 roots 是否指向真实语料目录（当前：{roots_txt}）\n"
        f"  2. 该目录下是否确有 {label} 文件"
        + (f"（大小上限 {max_mb} MB）" if max_mb else "")
    )


def find_scanned_pdfs(*, count: int = 3, max_mb: float = 10.0,
                      roots: Iterable[str | Path] | None = None) -> list[Path]:
    """挑出"需要走 OCR"的 PDF（文本层不可信/无文本层），用于冒烟测试。

    用引擎同一套判定（detect.pdf_text_trust），所以挑出来的必然真的会走 OCR，
    不会出现"随便挑个 PDF 结果它是文本型、测试前提不成立"。
    """
    from doc2md import detect

    cfg = load_cfg()
    min_chars = cfg.min_text_chars_per_page
    probe = cfg.text_pdf_probe_pages

    def is_scanned(p: Path) -> bool:
        if detect.sniff(p) is not detect.Kind.PDF:
            return False
        text_ok, _pages, _why = detect.pdf_text_trust(p, min_chars, probe)
        return not text_ok

    try:
        return find_samples(
            [".pdf"], count=count, roots=roots, max_mb=max_mb,
            predicate=is_scanned, what="扫描件 PDF",
        )
    except FileNotFoundError:
        return []
