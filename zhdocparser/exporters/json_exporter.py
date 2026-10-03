from __future__ import annotations

from pathlib import Path

from zhdocparser.schemas import Document


def export_document_json(document: Document, output_path: Path) -> None:
    output_path.write_text(document.model_dump_json(indent=2), encoding="utf-8")
