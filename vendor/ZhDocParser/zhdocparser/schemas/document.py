from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DocumentMetadata(BaseModel):
    source_file: str
    source_type: Literal["pdf", "docx"]
    language: str = "zh"
    page_count: int = 0
    title: str = ""
    doc_type: str = "general"
    source_name: str = ""
    section_count: int = 0
    table_count: int = 0
    chunk_count: int = 0


class Section(BaseModel):
    id: str
    heading: str = ""
    level: int = 0
    content: str = ""
    page_number: int = 0
    page_end: int = 0
    heading_path: list[str] = Field(default_factory=list)
    section_id_path: list[str] = Field(default_factory=list)
    parent_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Table(BaseModel):
    id: str
    page_number: int = 0
    page_end: int = 0
    title: str = ""
    rows: list[list[str]] = Field(default_factory=list)
    heading_path: list[str] = Field(default_factory=list)
    column_count: int = 0
    row_count: int = 0
    source_pages: list[int] = Field(default_factory=list)
    continued: bool = False
    context_before: list[str] = Field(default_factory=list)
    context_after: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Chunk(BaseModel):
    id: str
    text: str
    page_number: int = 0
    heading_path: list[str] = Field(default_factory=list)
    chunk_type: Literal["section", "table"] = "section"
    source_section_id: str | None = None
    source_table_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Page(BaseModel):
    page_number: int
    text: str = ""


class Document(BaseModel):
    metadata: DocumentMetadata
    pages: list[Page] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    chunks: list[Chunk] = Field(default_factory=list)
