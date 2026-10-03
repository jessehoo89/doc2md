from __future__ import annotations

import re

from zhdocparser.schemas import Chunk, Document

MAX_CHUNK_CHARS = 500


def chunk_document(document: Document, max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    chunks: list[Chunk] = []

    for section in document.sections:
        if not section.content.strip():
            continue
        for chunk_text in _split_text(section.content, max_chars=max_chars):
            chunks.append(
                Chunk(
                    id=f"chunk-{len(chunks) + 1}",
                    text=chunk_text,
                    page_number=section.page_number,
                    heading_path=section.heading_path,
                    chunk_type="section",
                    source_section_id=section.id,
                    metadata={
                        "source_file": document.metadata.source_file,
                        "source_name": document.metadata.source_name,
                        "document_title": document.metadata.title,
                        "doc_type": document.metadata.doc_type,
                        "section_heading": section.heading,
                        "heading_path_text": " > ".join(section.heading_path),
                        "section_level": section.level,
                        "section_id_path": section.section_id_path,
                        "parent_id": section.parent_id,
                        "page_range": [section.page_number, section.page_end or section.page_number],
                    },
                )
            )

    for table in document.tables:
        table_lines = [" | ".join(row) for row in table.rows if row]
        if not table_lines:
            continue
        chunks.append(
            Chunk(
                id=f"chunk-{len(chunks) + 1}",
                text="\n".join(table_lines),
                page_number=table.page_number,
                heading_path=table.heading_path,
                chunk_type="table",
                source_table_id=table.id,
                metadata={
                    "source_file": document.metadata.source_file,
                    "source_name": document.metadata.source_name,
                    "document_title": document.metadata.title,
                    "doc_type": document.metadata.doc_type,
                    "table_title": table.title,
                    "row_count": table.row_count,
                    "column_count": table.column_count,
                    "heading_path_text": " > ".join(table.heading_path),
                    "context_before": table.context_before,
                    "context_after": table.context_after,
                    "page_range": [table.page_number, table.page_end or table.page_number],
                    "source_pages": table.source_pages,
                    "continued": table.continued,
                },
            )
        )

    return chunks


def _split_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    normalized = text.strip()
    if not normalized:
        return []

    paragraphs = [part.strip() for part in re.split(r"\n{2,}", normalized) if part.strip()]
    if not paragraphs:
        return [normalized]

    chunks: list[str] = []
    current_parts: list[str] = []
    current_length = 0

    for paragraph in paragraphs:
        paragraph_length = len(paragraph)
        if current_parts and current_length + paragraph_length + 2 > max_chars:
            chunks.append("\n\n".join(current_parts))
            current_parts = [paragraph]
            current_length = paragraph_length
            continue

        current_parts.append(paragraph)
        current_length += paragraph_length if current_length == 0 else paragraph_length + 2

    if current_parts:
        chunks.append("\n\n".join(current_parts))

    return chunks
