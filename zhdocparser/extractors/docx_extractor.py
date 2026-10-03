from __future__ import annotations

from pathlib import Path

from docx import Document as DocxDocument
from docx.document import Document as DocxDocumentType
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph

from zhdocparser.extractors.base import BaseExtractor
from zhdocparser.schemas import Document, DocumentMetadata, Page, Section, Table


class DocxExtractor(BaseExtractor):
    supported_suffixes = (".docx",)

    def extract(self, source: Path) -> Document:
        doc = DocxDocument(source)
        sections: list[Section] = []
        tables: list[Table] = []
        page_text: list[str] = []
        heading_stack: list[str] = []
        section_stack: list[Section] = []
        title = source.stem
        section_counter = 0
        table_counter = 0
        current_section: Section | None = None

        for block in self._iter_block_items(doc):
            if isinstance(block, Paragraph):
                text = self._normalize_text(block.text)
                if not text:
                    continue

                style_name = block.style.name.lower() if block.style and block.style.name else ""
                level = self._detect_heading_level(style_name)

                if level > 0:
                    if current_section is not None:
                        sections.append(current_section)
                    heading_stack = heading_stack[: level - 1]
                    heading_stack.append(text)
                    section_stack = section_stack[: level - 1]
                    if title == source.stem:
                        title = text
                    section_counter += 1
                    section_id = f"sec-{section_counter}"
                    parent_id = section_stack[-1].id if section_stack else None
                    section_id_path = [*section_stack[-1].section_id_path, section_id] if section_stack else [section_id]
                    current_section = Section(
                        id=section_id,
                        heading=text,
                        level=level,
                        content="",
                        page_number=1,
                        page_end=1,
                        heading_path=heading_stack.copy(),
                        section_id_path=section_id_path,
                        parent_id=parent_id,
                    )
                    section_stack.append(current_section)
                    page_text.append(f"{'#' * level} {text}")
                    continue

                if current_section is None:
                    inferred_heading = title if title != source.stem else "Document"
                    heading_path = heading_stack.copy() or [inferred_heading]
                    section_counter += 1
                    section_id = f"sec-{section_counter}"
                    current_section = Section(
                        id=section_id,
                        heading=heading_path[-1],
                        level=1,
                        content="",
                        page_number=1,
                        page_end=1,
                        heading_path=heading_path,
                        section_id_path=[section_id],
                    )
                    if title == source.stem:
                        title = inferred_heading

                current_section.content = self._append_content(current_section.content, text)
                page_text.append(text)
                continue

            if isinstance(block, DocxTable):
                rows = self._extract_table_rows(block)
                if not rows:
                    continue
                table_counter += 1
                tables.append(
                    Table(
                        id=f"table-{table_counter}",
                        page_number=1,
                        page_end=1,
                        title=f"Table {table_counter}",
                        rows=rows,
                        heading_path=heading_stack.copy() or ([title] if title else []),
                        row_count=len(rows),
                        column_count=max(len(row) for row in rows),
                        source_pages=[1],
                        metadata={"source": "docx"},
                    )
                )
                page_text.append(self._table_to_text(rows))

        if current_section is not None:
            sections.append(current_section)

        return Document(
            metadata=DocumentMetadata(
                source_file=str(source),
                source_type="docx",
                page_count=1,
                title=title,
                doc_type=self._infer_doc_type(title, sections),
                source_name=source.name,
                section_count=len(sections),
                table_count=len(tables),
            ),
            pages=[Page(page_number=1, text="\n".join(page_text))],
            sections=sections,
            tables=tables,
        )

    @staticmethod
    def _iter_block_items(parent: DocxDocumentType):
        for child in parent.element.body.iterchildren():
            if isinstance(child, CT_P):
                yield Paragraph(child, parent)
            elif isinstance(child, CT_Tbl):
                yield DocxTable(child, parent)

    @staticmethod
    def _append_content(existing: str, new_text: str) -> str:
        if not existing:
            return new_text
        return f"{existing}\n\n{new_text}"

    @staticmethod
    def _normalize_text(text: str) -> str:
        return " ".join(text.replace("\xa0", " ").split())

    @staticmethod
    def _extract_table_rows(table: DocxTable) -> list[list[str]]:
        rows: list[list[str]] = []
        for row in table.rows:
            values = [DocxExtractor._normalize_text(cell.text) for cell in row.cells]
            if any(values):
                rows.append(values)
        return rows

    @staticmethod
    def _table_to_text(rows: list[list[str]]) -> str:
        return "\n".join(" | ".join(row) for row in rows if row)

    @staticmethod
    def _infer_doc_type(title: str, sections: list[Section]) -> str:
        corpus = " ".join([title, *[section.heading for section in sections if section.heading]])
        lowered = corpus.lower()
        if any(keyword in corpus for keyword in ("通知", "意见", "方案", "纪要", "报告")) or any(
            keyword in lowered for keyword in ("notice", "policy", "minutes")
        ):
            return "government"
        if any(keyword in corpus for keyword in ("试题", "试卷", "答案", "教材", "教案", "题库", "知识点")) or any(
            keyword in lowered for keyword in ("exam", "question", "answer", "lesson", "education")
        ):
            return "education"
        if any(keyword in corpus for keyword in ("分析", "季度", "年度", "经营", "财务", "复盘")) or any(
            keyword in lowered for keyword in ("analysis", "quarterly", "annual", "finance", "report")
        ):
            return "report"
        return "general"

    @staticmethod
    def _detect_heading_level(style_name: str) -> int:
        if "title" == style_name:
            return 1
        if "heading 1" in style_name or "标题 1" in style_name:
            return 1
        if "heading 2" in style_name or "标题 2" in style_name:
            return 2
        if "heading 3" in style_name or "标题 3" in style_name:
            return 3
        return 0
