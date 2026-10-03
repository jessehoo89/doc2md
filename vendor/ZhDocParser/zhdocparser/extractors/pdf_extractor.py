from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import pymupdf as fitz

from zhdocparser.extractors.base import BaseExtractor
from zhdocparser.schemas import Document, DocumentMetadata, Page, Section, Table


@dataclass(slots=True)
class PdfLine:
    text: str
    page_number: int
    bbox: tuple[float, float, float, float]
    font_size: float
    is_bold: bool
    page_height: float
    page_width: float


@dataclass(slots=True)
class PdfTableCandidate:
    bbox: tuple[float, float, float, float]
    rows: list[list[str]]
    page_number: int
    title: str = ""
    source: str = "grid"
    context_before: list[str] | None = None
    context_after: list[str] | None = None


class PdfExtractor(BaseExtractor):
    supported_suffixes = (".pdf",)

    def extract(self, source: Path) -> Document:
        pdf = fitz.open(source)
        pages: list[Page] = []
        sections: list[Section] = []
        tables: list[Table] = []
        page_lines: list[list[PdfLine]] = []
        page_tables: list[list[PdfTableCandidate]] = []
        all_lines: list[PdfLine] = []

        for index, page in enumerate(pdf, start=1):
            lines = self._extract_page_lines(page, index)
            table_candidates = self._extract_page_tables(page, index)
            page_lines.append(lines)
            page_tables.append(table_candidates)
            all_lines.extend(lines)

        header_footer_texts = self._detect_repeated_margin_text(all_lines)
        visible_lines: list[list[PdfLine]] = []

        for page_number, (lines, tables_on_page) in enumerate(zip(page_lines, page_tables, strict=True), start=1):
            filtered_lines = self._filter_lines(lines, tables_on_page, header_footer_texts)
            filtered_lines = self._order_lines_for_reading(filtered_lines)
            visible_lines.append(filtered_lines)
            text = "\n".join(line.text for line in filtered_lines).strip()
            pages.append(Page(page_number=page_number, text=text))

        body_font_size = self._detect_body_font_size(visible_lines)
        title = self._detect_title(source, visible_lines)
        doc_type = self._infer_doc_type(title, visible_lines)
        heading_stack: list[str] = []
        section_stack: list[Section] = []
        current_section: Section | None = None
        section_counter = 0
        table_counter = 0

        for lines, tables_on_page in zip(visible_lines, page_tables, strict=True):
            items = self._merge_page_items(lines, tables_on_page)

            for item_type, item in items:
                if item_type == "line":
                    line = item
                    if self._is_heading(line, body_font_size, title):
                        level = self._infer_heading_level(line, body_font_size)
                        if current_section is not None:
                            sections.append(current_section)
                        heading_stack = heading_stack[: level - 1]
                        heading_stack.append(line.text)
                        section_stack = section_stack[: level - 1]
                        section_counter += 1
                        section_id = f"sec-{section_counter}"
                        parent_id = section_stack[-1].id if section_stack else None
                        section_id_path = [*section_stack[-1].section_id_path, section_id] if section_stack else [section_id]
                        current_section = Section(
                            id=section_id,
                            heading=line.text,
                            level=level,
                            content="",
                            page_number=line.page_number,
                            page_end=line.page_number,
                            heading_path=heading_stack.copy(),
                            section_id_path=section_id_path,
                            parent_id=parent_id,
                            metadata={
                                "font_size": line.font_size,
                                "is_bold": line.is_bold,
                            },
                        )
                        section_stack.append(current_section)
                        continue

                    if current_section is None:
                        section_counter += 1
                        section_id = f"sec-{section_counter}"
                        current_section = Section(
                            id=section_id,
                            heading=title,
                            level=1,
                            content="",
                            page_number=line.page_number,
                            page_end=line.page_number,
                            heading_path=[title] if title else [],
                            section_id_path=[section_id],
                            metadata={"inferred": True},
                        )
                    current_section.content = self._append_content(current_section.content, line.text)
                    current_section.page_end = line.page_number
                    continue

                table_candidate = item
                table_counter += 1
                tables.append(
                    Table(
                        id=f"table-{table_counter}",
                        page_number=table_candidate.page_number,
                        page_end=table_candidate.page_number,
                        title=table_candidate.title or f"Table {table_counter}",
                        rows=table_candidate.rows,
                        heading_path=heading_stack.copy() or ([title] if title else []),
                        row_count=len(table_candidate.rows),
                        column_count=max((len(row) for row in table_candidate.rows), default=0),
                        source_pages=[table_candidate.page_number],
                        context_before=table_candidate.context_before,
                        context_after=table_candidate.context_after,
                        metadata={
                            "source": "pdf",
                            "detection": table_candidate.source,
                            "bbox": list(table_candidate.bbox),
                            "context_before": table_candidate.context_before,
                            "context_after": table_candidate.context_after,
                        },
                    )
                )

        if current_section is not None:
            sections.append(current_section)

        tables = self._merge_continued_tables(tables)

        return Document(
            metadata=DocumentMetadata(
                source_file=str(source),
                source_type="pdf",
                page_count=len(pages),
                title=title,
                doc_type=doc_type,
                source_name=source.name,
                section_count=len(sections),
                table_count=len(tables),
            ),
            pages=pages,
            sections=sections,
            tables=tables,
        )

    @staticmethod
    def _append_content(existing: str, new_text: str) -> str:
        if not existing:
            return new_text
        return f"{existing}\n{new_text}"

    @staticmethod
    def _normalize_text(text: str) -> str:
        return " ".join(text.replace("\xa0", " ").split())

    def _extract_page_lines(self, page: fitz.Page, page_number: int) -> list[PdfLine]:
        text_dict = page.get_text("dict")
        lines: list[PdfLine] = []

        # PyMuPDF 的 get_text() 返回的是**未旋转**坐标，而 page.rect 是旋转之后的
        # 显示尺寸。rotation != 0 的 PDF（横向存储 + 纵向显示）并不罕见 —— 例如
        # 由 WPS/Word 导出的红头文件。此时行 bbox 与 page_width/page_height 不在
        # 同一尺度上：「整行宽度 >= 页宽 * 0.65」「分栏间隙」这类判断会全线错位，
        # 把单栏正文误判成多栏并重排出完全错误的阅读顺序（正文被排到标题前面）。
        # 这里统一把 bbox 归一到显示坐标系，与 page.rect 保持一致。
        to_display = page.rotation_matrix if page.rotation else None

        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                line_text = self._normalize_text("".join(span.get("text", "") for span in spans))
                if line_text:
                    max_font = max((span.get("size", 0.0) for span in spans), default=0.0)
                    is_bold = any(
                        "bold" in str(span.get("font", "")).lower() or span.get("flags", 0) & 16
                        for span in spans
                    )
                    bbox = tuple(line.get("bbox", (0.0, 0.0, 0.0, 0.0)))
                    if to_display is not None:
                        bbox = tuple(fitz.Rect(bbox) * to_display)
                    lines.append(
                        PdfLine(
                            text=line_text,
                            page_number=page_number,
                            bbox=bbox,
                            font_size=max_font,
                            is_bold=is_bold,
                            page_height=page.rect.height,
                            page_width=page.rect.width,
                        )
                    )

        return lines

    def _extract_page_tables(self, page: fitz.Page, page_number: int) -> list[PdfTableCandidate]:
        table_candidates: list[PdfTableCandidate] = []
        lines_for_titles = self._extract_page_lines(page, page_number)
        if hasattr(page, "find_tables"):
            finder = page.find_tables()
            for table in finder.tables:
                rows: list[list[str]] = []
                for row in table.rows:
                    values: list[str] = []
                    for cell in row.cells:
                        if cell is None:
                            values.append("")
                            continue
                        values.append(self._normalize_text(page.get_textbox(self._shrink_bbox(cell, 2.0))))
                    if any(values):
                        rows.append(values)

                if not rows:
                    continue

                title = self._infer_table_title(table.bbox, lines_for_titles)
                context_before, context_after = self._infer_table_context(table.bbox, lines_for_titles)
                table_candidates.append(
                    PdfTableCandidate(
                        bbox=table.bbox,
                        rows=rows,
                        page_number=page_number,
                        title=title,
                        source="grid",
                        context_before=context_before,
                        context_after=context_after,
                    )
                )

        table_candidates.extend(self._extract_text_tables(lines_for_titles, table_candidates, page_number))
        return table_candidates

    @staticmethod
    def _detect_repeated_margin_text(lines: list[PdfLine]) -> set[str]:
        occurrences: dict[str, set[int]] = {}

        for line in lines:
            normalized = PdfExtractor._normalize_text(line.text)
            if not normalized or len(normalized) > 80:
                continue
            if line.font_size > 12.5:
                continue
            if PdfExtractor._heading_pattern_level(normalized) > 0:
                continue
            top_margin = line.bbox[1] <= min(72.0, line.page_height * 0.12)
            bottom_margin = line.bbox[3] >= line.page_height - min(72.0, line.page_height * 0.12)
            if not top_margin and not bottom_margin:
                continue
            occurrences.setdefault(normalized, set()).add(line.page_number)

        return {text for text, pages in occurrences.items() if len(pages) >= 2}

    def _filter_lines(
        self,
        lines: list[PdfLine],
        tables: list[PdfTableCandidate],
        repeated_margin_texts: set[str],
    ) -> list[PdfLine]:
        filtered: list[PdfLine] = []

        for line in lines:
            normalized = self._normalize_text(line.text)
            if normalized in repeated_margin_texts:
                continue
            if any(self._intersects(line.bbox, table.bbox) for table in tables):
                continue
            filtered.append(line)

        return filtered

    @staticmethod
    def _intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
        return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])

    @staticmethod
    def _detect_body_font_size(pages: list[list[PdfLine]]) -> float:
        font_sizes = [line.font_size for page_lines in pages for line in page_lines if line.font_size > 0]
        return median(font_sizes) if font_sizes else 12.0

    @staticmethod
    def _detect_title(source: Path, pages: list[list[PdfLine]]) -> str:
        for page_lines in pages:
            if page_lines:
                return page_lines[0].text
        return source.stem

    @staticmethod
    def _infer_doc_type(title: str, pages: list[list[PdfLine]]) -> str:
        corpus = " ".join([title, *[line.text for page in pages for line in page[:10]]])
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
    def _shrink_bbox(
        bbox: tuple[float, float, float, float],
        inset: float,
    ) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = bbox
        return (x0 + inset, y0 + inset, x1 - inset, y1 - inset)

    def _is_heading(self, line: PdfLine, body_font_size: float, title: str) -> bool:
        text = line.text
        if not text:
            return False
        if line.page_number == 1 and text == title:
            return True
        if self._heading_pattern_level(text) > 0:
            return True
        if len(text) > 40 or text.endswith(("。", "；", "！", "？")):
            return False
        return line.font_size >= body_font_size + 1.0 or (line.is_bold and len(text) <= 24)

    def _infer_heading_level(self, line: PdfLine, body_font_size: float) -> int:
        if line.page_number == 1 and line.text:
            first_page_title_level = self._heading_pattern_level(line.text)
            if first_page_title_level == 0 and line.font_size >= body_font_size:
                return 1
        pattern_level = self._heading_pattern_level(line.text)
        if pattern_level > 0:
            return pattern_level
        if line.font_size >= body_font_size + 3:
            return 1
        if line.font_size >= body_font_size + 1.5:
            return 2
        return 3

    @staticmethod
    def _heading_pattern_level(text: str) -> int:
        patterns = [
            (1, r"^第[一二三四五六七八九十百千万0-9]+[章节编部分篇]"),
            (1, r"^[一二三四五六七八九十]+[、.]"),
            (2, r"^（[一二三四五六七八九十0-9]+）"),
            (2, r"^\([一二三四五六七八九十0-9]+\)"),
            # 更具体的模式必须排在更宽泛的前面：`\d+\.\d+\.\d+` 先于
            # `\d+\.\d+`，`\d+\.\d+` 先于 `\d+[、.]`。原顺序里
            # `^\d+[、.]` 排在前面，`1.1.1 xxx` 会被它的前缀 `1.` 抢先匹配，
            # 于是三级标题被降级成二级；`^\d+[、.]` 也会把 `1.5 倍…` 这类
            # 正文误判成编号标题，故加 `(?!\d)` 排除小数。
            (3, r"^\d+\.\d+\.\d+(?!\d)"),
            (2, r"^\d+\.\d+(?!\d)"),
            (2, r"^\d+[、.](?!\d)"),
            (3, r"^[①②③④⑤⑥⑦⑧⑨⑩]"),
        ]
        for level, pattern in patterns:
            if re.match(pattern, text):
                return level
        return 0

    def _infer_table_title(self, bbox: tuple[float, float, float, float], lines: list[PdfLine]) -> str:
        candidates = [
            line.text
            for line in lines
            if line.bbox[3] <= bbox[1] and bbox[1] - line.bbox[3] <= 48
        ]
        if not candidates:
            return ""
        for text in reversed(candidates):
            if "表" in text or "table" in text.lower():
                return text
        return candidates[-1]

    @staticmethod
    def _order_lines_for_reading(lines: list[PdfLine]) -> list[PdfLine]:
        if len(lines) <= 2:
            return sorted(lines, key=lambda line: (line.bbox[1], line.bbox[0]))

        page_width = lines[0].page_width
        full_width_threshold = page_width * 0.65
        wide_lines = [line for line in lines if (line.bbox[2] - line.bbox[0]) >= full_width_threshold]
        boundaries = sorted({line.bbox[1] for line in wide_lines} | {line.bbox[3] for line in wide_lines})

        if not boundaries:
            return PdfExtractor._sort_column_region(lines)

        regions: list[tuple[float, float]] = []
        top = min(line.bbox[1] for line in lines)
        for boundary in boundaries:
            if boundary > top:
                regions.append((top, boundary))
            top = boundary
        bottom = max(line.bbox[3] for line in lines)
        if bottom > top:
            regions.append((top, bottom))

        ordered: list[PdfLine] = []
        consumed: set[int] = set()

        for start, end in regions:
            region_lines = [
                line for idx, line in enumerate(lines)
                if idx not in consumed and line.bbox[1] >= start - 1 and line.bbox[3] <= end + 1
            ]
            if not region_lines:
                continue
            region_wide = [line for line in region_lines if (line.bbox[2] - line.bbox[0]) >= full_width_threshold]
            if region_wide:
                region_ordered = sorted(region_lines, key=lambda line: (line.bbox[1], line.bbox[0]))
            else:
                region_ordered = PdfExtractor._sort_column_region(region_lines)
            for line in region_ordered:
                original_index = lines.index(line)
                consumed.add(original_index)
                ordered.append(line)

        if len(ordered) != len(lines):
            leftovers = [line for idx, line in enumerate(lines) if idx not in consumed]
            ordered.extend(sorted(leftovers, key=lambda line: (line.bbox[1], line.bbox[0])))

        return ordered

    @staticmethod
    def _sort_column_region(lines: list[PdfLine]) -> list[PdfLine]:
        if not lines:
            return []

        sorted_by_x = sorted(lines, key=lambda line: line.bbox[0])
        columns: list[list[PdfLine]] = [[sorted_by_x[0]]]
        current_anchor = sorted_by_x[0].bbox[0]
        column_gap_threshold = max(sorted_by_x[0].page_width * 0.12, 48.0)

        for line in sorted_by_x[1:]:
            if line.bbox[0] - current_anchor > column_gap_threshold:
                columns.append([line])
                current_anchor = line.bbox[0]
                continue
            columns[-1].append(line)
            current_anchor = min(current_anchor, columns[-1][0].bbox[0])

        if len(columns) == 1:
            return sorted(lines, key=lambda line: (line.bbox[1], line.bbox[0]))

        ordered: list[PdfLine] = []
        for column in columns:
            ordered.extend(sorted(column, key=lambda line: (line.bbox[1], line.bbox[0])))
        return ordered

    @staticmethod
    def _infer_table_context(
        bbox: tuple[float, float, float, float],
        lines: list[PdfLine],
    ) -> tuple[list[str], list[str]]:
        before = [
            line.text
            for line in lines
            if line.bbox[3] <= bbox[1] and bbox[1] - line.bbox[3] <= 84
        ]
        after = [
            line.text
            for line in lines
            if line.bbox[1] >= bbox[3] and line.bbox[1] - bbox[3] <= 48
        ]
        return before[-2:], after[:2]

    @staticmethod
    def _merge_page_items(
        lines: list[PdfLine],
        tables: list[PdfTableCandidate],
    ) -> list[tuple[str, PdfLine | PdfTableCandidate]]:
        merged: list[tuple[str, PdfLine | PdfTableCandidate]] = [("line", line) for line in lines]

        for table in sorted(tables, key=lambda candidate: candidate.bbox[1]):
            insertion_index = 0
            for idx, line in enumerate(lines):
                if line.bbox[3] <= table.bbox[1]:
                    insertion_index = idx + 1
            merged.insert(insertion_index, ("table", table))

        return merged

    def _extract_text_tables(
        self,
        lines: list[PdfLine],
        existing_tables: list[PdfTableCandidate],
        page_number: int,
    ) -> list[PdfTableCandidate]:
        occupied = [table.bbox for table in existing_tables]
        grouped_rows = self._group_lines_by_row(lines)
        candidates: list[PdfTableCandidate] = []
        current_rows: list[list[PdfLine]] = []

        def flush_rows() -> None:
            nonlocal current_rows
            if len(current_rows) < 3:
                current_rows = []
                return

            modal_columns = max((len(row) for row in current_rows), default=0)
            if modal_columns < 2:
                current_rows = []
                return

            filtered_rows = [row for row in current_rows if len(row) == modal_columns]
            if len(filtered_rows) < 3:
                current_rows = []
                return

            anchors = [row[0].bbox[0] for row in filtered_rows]
            if max(anchors) - min(anchors) > 24:
                current_rows = []
                return

            rows = [[cell.text for cell in row] for row in filtered_rows]
            bbox = self._union_bbox(filtered_rows)
            if any(self._intersects(bbox, existing_bbox) for existing_bbox in occupied):
                current_rows = []
                return

            title = self._infer_table_title(bbox, lines)
            if len(filtered_rows) < 3 and not title:
                current_rows = []
                return

            context_before, context_after = self._infer_table_context(bbox, lines)
            candidates.append(
                PdfTableCandidate(
                    bbox=bbox,
                    rows=rows,
                    page_number=page_number,
                    title=title,
                    source="text",
                    context_before=context_before,
                    context_after=context_after,
                )
            )
            occupied.append(bbox)
            current_rows = []

        for row in grouped_rows:
            sorted_row = sorted(row, key=lambda line: line.bbox[0])
            if len(sorted_row) >= 2 and self._looks_like_table_row(sorted_row):
                if current_rows and not self._rows_align(current_rows[-1], sorted_row):
                    flush_rows()
                current_rows.append(sorted_row)
                continue
            flush_rows()

        flush_rows()
        return candidates

    @staticmethod
    def _group_lines_by_row(lines: list[PdfLine], tolerance: float = 6.0) -> list[list[PdfLine]]:
        grouped: list[list[PdfLine]] = []
        for line in sorted(lines, key=lambda item: (item.bbox[1], item.bbox[0])):
            if not grouped:
                grouped.append([line])
                continue
            last_group = grouped[-1]
            last_y = sum(item.bbox[1] for item in last_group) / len(last_group)
            if abs(line.bbox[1] - last_y) <= tolerance:
                last_group.append(line)
            else:
                grouped.append([line])
        return grouped

    @staticmethod
    def _looks_like_table_row(row: list[PdfLine]) -> bool:
        if len(row) < 2:
            return False
        gaps = [row[index + 1].bbox[0] - row[index].bbox[2] for index in range(len(row) - 1)]
        return any(gap >= 24 for gap in gaps)

    @staticmethod
    def _rows_align(previous_row: list[PdfLine], current_row: list[PdfLine], tolerance: float = 20.0) -> bool:
        if len(previous_row) != len(current_row):
            return False
        return all(abs(prev.bbox[0] - curr.bbox[0]) <= tolerance for prev, curr in zip(previous_row, current_row))

    @staticmethod
    def _union_bbox(rows: list[list[PdfLine]]) -> tuple[float, float, float, float]:
        all_lines = [line for row in rows for line in row]
        return (
            min(line.bbox[0] for line in all_lines),
            min(line.bbox[1] for line in all_lines),
            max(line.bbox[2] for line in all_lines),
            max(line.bbox[3] for line in all_lines),
        )

    def _merge_continued_tables(self, tables: list[Table]) -> list[Table]:
        if not tables:
            return []

        merged: list[Table] = [tables[0]]
        for table in tables[1:]:
            previous = merged[-1]
            if self._should_merge_tables(previous, table):
                previous.rows = self._merge_table_rows(previous.rows, table.rows)
                previous.page_end = table.page_end or table.page_number
                previous.row_count = len(previous.rows)
                previous.source_pages = [*previous.source_pages, *[page for page in table.source_pages if page not in previous.source_pages]]
                previous.continued = True
                previous.context_after = table.context_after or previous.context_after
                previous.metadata["continued"] = True
                previous.metadata["source_pages"] = previous.source_pages
                continue
            merged.append(table)
        return merged

    @staticmethod
    def _should_merge_tables(previous: Table, current: Table) -> bool:
        if current.page_number != previous.page_end + 1:
            return False
        if previous.column_count != current.column_count:
            return False
        if previous.heading_path != current.heading_path:
            return False
        previous_title = previous.title.lower().strip()
        current_title = current.title.lower().strip()
        if previous_title and current_title and previous_title != current_title:
            return False
        if not previous.rows or not current.rows:
            return False
        return previous.rows[0] == current.rows[0]

    @staticmethod
    def _merge_table_rows(previous_rows: list[list[str]], current_rows: list[list[str]]) -> list[list[str]]:
        if not previous_rows:
            return current_rows
        if not current_rows:
            return previous_rows
        if previous_rows[0] == current_rows[0]:
            return [*previous_rows, *current_rows[1:]]
        return [*previous_rows, *current_rows]
