"""ZhDocParser package."""

from typing import Any

__all__ = ["__version__", "parse_and_export", "parse_dir", "parse_file"]

__version__ = "0.3.0"

_SDK_EXPORTS = ("parse_and_export", "parse_dir", "parse_file")


def __getattr__(name: str) -> Any:
    """惰性暴露 SDK 入口（PEP 562）。

    顶层 `from zhdocparser.sdk import ...` 会让**任何**子模块导入都连带载入
    sdk → service → extractors.factory → docx_extractor → python-docx。
    只做 PDF 的调用方（比如 `from zhdocparser.extractors.pdf_extractor import
    PdfExtractor`）不该因此被拖上 python-docx / lxml 这一整套依赖，
    也不该在 import 时付出它们的启动开销。
    """
    if name in _SDK_EXPORTS:
        from zhdocparser import sdk

        return getattr(sdk, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
