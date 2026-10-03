from __future__ import annotations

from pathlib import Path

from zhdocparser.schemas import Document


class ExtractionError(RuntimeError):
    """Raised when a file cannot be extracted."""


class BaseExtractor:
    supported_suffixes: tuple[str, ...] = ()

    def extract(self, source: Path) -> Document:
        raise NotImplementedError
