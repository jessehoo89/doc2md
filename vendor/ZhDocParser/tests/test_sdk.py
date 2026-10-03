from __future__ import annotations

from docx import Document as DocxDocument

from zhdocparser.config import ParserConfig
from zhdocparser.sdk import parse_dir, parse_file


def test_sdk_parse_file_respects_config(tmp_path) -> None:
    source = tmp_path / "demo.docx"
    doc = DocxDocument()
    doc.add_heading("General Title", level=1)
    doc.add_paragraph("A" * 160)
    doc.add_paragraph("B" * 160)
    doc.save(source)

    document = parse_file(source, ParserConfig(doc_type_override="report", max_chunk_chars=150))

    assert document.metadata.doc_type == "report"
    assert len(document.chunks) >= 2
    assert document.metadata.chunk_count == len(document.chunks)


def test_sdk_parse_dir_returns_documents(tmp_path) -> None:
    source_dir = tmp_path / "sources"
    source_dir.mkdir()

    first = DocxDocument()
    first.add_heading("Notice", level=1)
    first.add_paragraph("Alpha")
    first.save(source_dir / "a.docx")

    second = DocxDocument()
    second.add_heading("Quarterly Report", level=1)
    second.add_paragraph("Beta")
    second.save(source_dir / "b.docx")

    documents = parse_dir(source_dir, ParserConfig(), recursive=False)

    assert len(documents) == 2
    assert {document.metadata.title for document in documents} == {"Notice", "Quarterly Report"}
