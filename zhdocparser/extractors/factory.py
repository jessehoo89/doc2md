from __future__ import annotations

from pathlib import Path

from zhdocparser.extractors.base import ExtractionError
from zhdocparser.extractors.docx_extractor import DocxExtractor
from zhdocparser.extractors.pdf_extractor import PdfExtractor
from zhdocparser.schemas import Document

_EXTRACTORS = [
    PdfExtractor(),
    DocxExtractor(),
]


def parse_document(source: Path) -> Document:
    suffix = source.suffix.lower()
    for extractor in _EXTRACTORS:
        if suffix in extractor.supported_suffixes:
            return extractor.extract(source)
    raise ExtractionError(f"Unsupported file type: {suffix}")
