from __future__ import annotations

from pydantic import BaseModel, Field


class ParserConfig(BaseModel):
    doc_type_override: str | None = None
    max_chunk_chars: int = Field(default=500, ge=100, le=5000)
    export_markdown: bool = True
    export_json: bool = True
    export_chunks: bool = True

    def has_any_export(self) -> bool:
        return self.export_markdown or self.export_json or self.export_chunks
