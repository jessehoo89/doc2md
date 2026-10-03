from __future__ import annotations

from pathlib import Path

from zhdocparser.schemas import Document


def export_chunks_json(document: Document, output_path: Path) -> None:
    payload = [chunk.model_dump() for chunk in document.chunks]
    output_path.write_text(_to_json(payload), encoding="utf-8")


def _to_json(payload: list[dict]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
