from __future__ import annotations

from pathlib import Path

from zhdocparser.schemas import Document


def export_document_markdown(document: Document, output_path: Path) -> None:
    lines: list[str] = [f"# {document.metadata.title}".strip(), ""]

    for index, section in enumerate(document.sections):
        is_duplicate_title = (
            index == 0
            and section.heading == document.metadata.title
        )
        if is_duplicate_title:
            if section.content:
                lines.append(section.content)
                lines.append("")
            continue
        if section.level > 0 and section.heading:
            lines.append(f"{'#' * section.level} {section.heading}")
            lines.append("")
        if section.content:
            lines.append(section.content)
            lines.append("")

    if document.tables:
        lines.append("## Tables")
        lines.append("")
        for table in document.tables:
            lines.append(f"### {table.title or table.id}")
            if table.heading_path:
                lines.append(f"_Heading Path: {' > '.join(table.heading_path)}_")
                lines.append("")
            lines.append("")
            if table.rows:
                header = table.rows[0]
                lines.append("| " + " | ".join(header) + " |")
                lines.append("| " + " | ".join(["---"] * len(header)) + " |")
                for row in table.rows[1:]:
                    lines.append("| " + " | ".join(row) + " |")
                lines.append("")

    output_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
