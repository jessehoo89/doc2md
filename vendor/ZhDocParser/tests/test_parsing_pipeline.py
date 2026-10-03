from __future__ import annotations

from pathlib import Path

import fitz
from docx import Document as DocxDocument

from zhdocparser.chunkers.basic import chunk_document
from zhdocparser.extractors.docx_extractor import DocxExtractor
from zhdocparser.extractors.pdf_extractor import PdfExtractor
from zhdocparser.schemas import Document, DocumentMetadata, Section, Table


def test_chunk_document_preserves_metadata_and_splits_long_text() -> None:
    document = Document(
        metadata=DocumentMetadata(
            source_file="demo.pdf",
            source_type="pdf",
            title="Demo",
            doc_type="general",
            source_name="demo.pdf",
        ),
        sections=[
            Section(
                id="sec-1",
                heading="Intro",
                level=1,
                content=("段落一。" * 80) + "\n\n" + ("段落二。" * 80),
                page_number=1,
                page_end=2,
                heading_path=["Intro"],
                section_id_path=["sec-1"],
                parent_id=None,
                metadata={},
            )
        ],
        tables=[
            Table(
                id="table-1",
                title="状态表",
                page_number=1,
                page_end=2,
                rows=[["模块", "状态"], ["标题恢复", "完成"]],
                heading_path=["Intro"],
                row_count=2,
                column_count=2,
                source_pages=[1, 2],
                continued=True,
                context_before=["Intro"],
            )
        ],
    )

    chunks = chunk_document(document)

    assert len(chunks) == 3
    assert chunks[0].metadata["document_title"] == "Demo"
    assert chunks[0].metadata["source_name"] == "demo.pdf"
    assert chunks[0].metadata["section_heading"] == "Intro"
    assert chunks[0].metadata["section_level"] == 1
    assert chunks[0].metadata["section_id_path"] == ["sec-1"]
    assert chunks[0].metadata["page_range"] == [1, 2]
    assert chunks[-1].chunk_type == "table"
    assert chunks[-1].metadata["table_title"] == "状态表"
    assert chunks[-1].metadata["context_before"] == ["Intro"]
    assert chunks[-1].metadata["continued"] is True
    assert chunks[-1].metadata["source_pages"] == [1, 2]


def test_docx_extractor_builds_sections_and_tables(tmp_path: Path) -> None:
    source = tmp_path / "demo.docx"
    doc = DocxDocument()
    doc.add_heading("通知", level=1)
    doc.add_paragraph("这是第一段。")
    doc.add_heading("实施范围", level=2)
    doc.add_paragraph("这是第二段。")
    table = doc.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "模块"
    table.rows[0].cells[1].text = "状态"
    table.rows[1].cells[0].text = "表格抽取"
    table.rows[1].cells[1].text = "完成"
    doc.save(source)

    document = DocxExtractor().extract(source)

    assert document.metadata.title == "通知"
    assert document.metadata.doc_type == "government"
    assert document.metadata.section_count == 2
    assert document.metadata.table_count == 1
    assert len(document.sections) == 2
    assert document.sections[0].content == "这是第一段。"
    assert document.sections[1].heading_path == ["通知", "实施范围"]
    assert document.sections[1].parent_id == document.sections[0].id
    assert document.sections[1].section_id_path == [document.sections[0].id, document.sections[1].id]
    assert len(document.tables) == 1
    assert document.tables[0].rows[0] == ["模块", "状态"]


def test_pdf_extractor_filters_headers_and_extracts_tables(tmp_path: Path) -> None:
    source = tmp_path / "demo.pdf"
    pdf = fitz.open()
    for page_index in range(2):
        page = pdf.new_page()
        page.insert_text((72, 40), "固定页眉", fontsize=10, fontname="helv")
        page.insert_text((72, 80), "1. Overview", fontsize=18, fontname="helv")
        page.insert_text((72, 110), f"Body line page {page_index + 1}", fontsize=12, fontname="helv")
        page.insert_text((72, 780), "固定页脚", fontsize=10, fontname="helv")
        if page_index == 0:
            x0, y0, width, height = 72, 160, 240, 90
            for row in range(4):
                y = y0 + row * (height / 3)
                page.draw_line((x0, y), (x0 + width, y))
            for col in range(3):
                x = x0 + col * (width / 2)
                page.draw_line((x, y0), (x, y0 + height))
            page.insert_text((80, 184), "Module", fontsize=10, fontname="helv")
            page.insert_text((200, 184), "Status", fontsize=10, fontname="helv")
            page.insert_text((80, 214), "Table", fontsize=10, fontname="helv")
            page.insert_text((200, 214), "Done", fontsize=10, fontname="helv")
            page.insert_text((80, 244), "Chunk", fontsize=10, fontname="helv")
            page.insert_text((200, 244), "Ready", fontsize=10, fontname="helv")
    pdf.save(source)
    pdf.close()

    document = PdfExtractor().extract(source)

    assert document.metadata.title == "1. Overview"
    assert [page.page_number for page in document.pages] == [1, 2]
    assert "固定页眉" not in document.pages[0].text
    assert "固定页脚" not in document.pages[0].text
    assert document.sections[0].heading == "1. Overview"
    assert "Body line page 1" in document.sections[0].content
    assert len(document.tables) == 1
    assert document.tables[0].rows[0] == ["Module", "Status"]


def test_pdf_extractor_recovers_two_column_reading_order(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 60), "Quarterly Analysis Report", fontsize=20, fontname="helv")
    page.insert_text((72, 100), "Left column line 1", fontsize=11, fontname="helv")
    page.insert_text((72, 124), "Left column line 2", fontsize=11, fontname="helv")
    page.insert_text((320, 100), "Right column line 1", fontsize=11, fontname="helv")
    page.insert_text((320, 124), "Right column line 2", fontsize=11, fontname="helv")
    pdf.save(source)
    pdf.close()

    document = PdfExtractor().extract(source)

    assert document.metadata.doc_type == "report"
    assert document.sections[0].content == (
        "Left column line 1\nLeft column line 2\nRight column line 1\nRight column line 2"
    )


def test_pdf_extractor_binds_table_context_and_doc_type(tmp_path: Path) -> None:
    source = tmp_path / "education.pdf"
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 60), "Exam Lesson Overview", fontsize=18, fontname="helv")
    page.insert_text((72, 90), "Question distribution is shown below.", fontsize=12, fontname="helv")
    page.insert_text((72, 118), "Table 1 Question Stats", fontsize=12, fontname="helv")
    x0, y0, width, height = 72, 150, 240, 90
    for row in range(4):
        y = y0 + row * (height / 3)
        page.draw_line((x0, y), (x0 + width, y))
    for col in range(3):
        x = x0 + col * (width / 2)
        page.draw_line((x, y0), (x, y0 + height))
    page.insert_text((80, 174), "Type", fontsize=10, fontname="helv")
    page.insert_text((200, 174), "Count", fontsize=10, fontname="helv")
    page.insert_text((80, 204), "Choice", fontsize=10, fontname="helv")
    page.insert_text((200, 204), "10", fontsize=10, fontname="helv")
    page.insert_text((80, 234), "Essay", fontsize=10, fontname="helv")
    page.insert_text((200, 234), "4", fontsize=10, fontname="helv")
    page.insert_text((72, 260), "Post-table note", fontsize=12, fontname="helv")
    pdf.save(source)
    pdf.close()

    document = PdfExtractor().extract(source)

    assert document.metadata.doc_type == "education"
    assert document.tables[0].title == "Table 1 Question Stats"
    assert document.tables[0].context_before == ["Question distribution is shown below.", "Table 1 Question Stats"]
    assert document.tables[0].context_after == ["Post-table note"]


def test_pdf_extractor_merges_continued_tables_across_pages(tmp_path: Path) -> None:
    source = tmp_path / "continued.pdf"
    pdf = fitz.open()
    for page_index in range(2):
        page = pdf.new_page()
        page.insert_text((72, 60), "Quarterly Report", fontsize=18, fontname="helv")
        page.insert_text((72, 90), "Table 1 Revenue Breakdown", fontsize=12, fontname="helv")
        x0, y0, width, height = 72, 120, 240, 90
        for row in range(4):
            y = y0 + row * (height / 3)
            page.draw_line((x0, y), (x0 + width, y))
        for col in range(3):
            x = x0 + col * (width / 2)
            page.draw_line((x, y0), (x, y0 + height))
        page.insert_text((80, 144), "Region", fontsize=10, fontname="helv")
        page.insert_text((200, 144), "Value", fontsize=10, fontname="helv")
        if page_index == 0:
            page.insert_text((80, 174), "North", fontsize=10, fontname="helv")
            page.insert_text((200, 174), "120", fontsize=10, fontname="helv")
            page.insert_text((80, 204), "South", fontsize=10, fontname="helv")
            page.insert_text((200, 204), "95", fontsize=10, fontname="helv")
        else:
            page.insert_text((80, 174), "East", fontsize=10, fontname="helv")
            page.insert_text((200, 174), "88", fontsize=10, fontname="helv")
            page.insert_text((80, 204), "West", fontsize=10, fontname="helv")
            page.insert_text((200, 204), "102", fontsize=10, fontname="helv")
    pdf.save(source)
    pdf.close()

    document = PdfExtractor().extract(source)

    assert len(document.tables) == 1
    assert document.tables[0].continued is True
    assert document.tables[0].page_end == 2
    assert document.tables[0].source_pages == [1, 2]
    assert document.tables[0].rows == [
        ["Region", "Value"],
        ["North", "120"],
        ["South", "95"],
        ["East", "88"],
        ["West", "102"],
    ]


def test_pdf_extractor_detects_text_aligned_table_without_grid_lines(tmp_path: Path) -> None:
    source = tmp_path / "weak_table.pdf"
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 60), "Exam Lesson Overview", fontsize=18, fontname="helv")
    page.insert_text((72, 90), "Table 2 Topic Coverage", fontsize=12, fontname="helv")
    page.insert_text((72, 120), "Topic", fontsize=11, fontname="helv")
    page.insert_text((220, 120), "Count", fontsize=11, fontname="helv")
    page.insert_text((72, 144), "Grammar", fontsize=11, fontname="helv")
    page.insert_text((220, 144), "6", fontsize=11, fontname="helv")
    page.insert_text((72, 168), "Reading", fontsize=11, fontname="helv")
    page.insert_text((220, 168), "4", fontsize=11, fontname="helv")
    pdf.save(source)
    pdf.close()

    document = PdfExtractor().extract(source)

    assert len(document.tables) == 1
    assert document.tables[0].metadata["detection"] == "text"
    assert document.tables[0].rows == [["Topic", "Count"], ["Grammar", "6"], ["Reading", "4"]]
