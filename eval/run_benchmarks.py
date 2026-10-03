from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from zhdocparser import parse_file
from zhdocparser.config import ParserConfig

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from fixtures import create_continued_table_fixture, create_report_fixture, create_text_table_fixture

ROOT = Path(__file__).resolve().parents[1]
REFERENCES_DIR = ROOT / "eval" / "references"
SAMPLES_DIR = ROOT / "samples"


def ensure_sample_documents() -> None:
    sample_pdf = SAMPLES_DIR / "example.pdf"
    sample_docx = SAMPLES_DIR / "example.docx"
    if sample_pdf.exists() and sample_docx.exists():
        return
    runpy.run_path(str(ROOT / "scripts" / "create_sample_documents.py"), run_name="__main__")


def load_reference(name: str) -> dict:
    return json.loads((REFERENCES_DIR / name).read_text(encoding="utf-8"))


def compare_document(case_name: str, document, expected: dict) -> list[str]:
    issues: list[str] = []
    metadata = document.metadata

    checks = {
        "title": metadata.title,
        "doc_type": metadata.doc_type,
        "page_count": metadata.page_count,
        "section_count": metadata.section_count,
        "table_count": metadata.table_count,
        "chunk_count": metadata.chunk_count,
    }
    for key, actual in checks.items():
        if key in expected and actual != expected[key]:
            issues.append(f"{case_name}: expected {key}={expected[key]!r}, got {actual!r}")

    if expected.get("first_section_heading") and document.sections:
        if document.sections[0].heading != expected["first_section_heading"]:
            issues.append(
                f"{case_name}: expected first_section_heading={expected['first_section_heading']!r}, "
                f"got {document.sections[0].heading!r}"
            )

    if expected.get("first_section_content") and document.sections:
        if document.sections[0].content != expected["first_section_content"]:
            issues.append(f"{case_name}: first section content mismatch")

    if expected.get("first_table_title") and document.tables:
        if document.tables[0].title != expected["first_table_title"]:
            issues.append(f"{case_name}: expected first_table_title={expected['first_table_title']!r}, got {document.tables[0].title!r}")

    if expected.get("first_table_detection") and document.tables:
        actual_detection = document.tables[0].metadata.get("detection")
        if actual_detection != expected["first_table_detection"]:
            issues.append(f"{case_name}: expected first_table_detection={expected['first_table_detection']!r}, got {actual_detection!r}")

    if "first_table_rows" in expected and document.tables:
        if document.tables[0].rows != expected["first_table_rows"]:
            issues.append(f"{case_name}: first table rows mismatch")

    if "first_table_continued" in expected and document.tables:
        if document.tables[0].continued != expected["first_table_continued"]:
            issues.append(f"{case_name}: expected continued={expected['first_table_continued']!r}, got {document.tables[0].continued!r}")

    if "first_table_source_pages" in expected and document.tables:
        if document.tables[0].source_pages != expected["first_table_source_pages"]:
            issues.append(f"{case_name}: expected source_pages={expected['first_table_source_pages']!r}, got {document.tables[0].source_pages!r}")

    return issues


def run() -> int:
    ensure_sample_documents()
    issues: list[str] = []

    cases: list[tuple[str, Path, dict]] = [
        ("example_pdf", SAMPLES_DIR / "example.pdf", load_reference("example_pdf.expected.json")),
        ("example_docx", SAMPLES_DIR / "example.docx", load_reference("example_docx.expected.json")),
    ]

    with TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        report_path = temp_root / "report_fixture.pdf"
        text_table_path = temp_root / "text_table_fixture.pdf"
        continued_path = temp_root / "continued_table_fixture.pdf"
        create_report_fixture(report_path)
        create_text_table_fixture(text_table_path)
        create_continued_table_fixture(continued_path)

        cases.extend(
            [
                ("report_fixture", report_path, load_reference("report_fixture.expected.json")),
                ("text_table_fixture", text_table_path, load_reference("text_table_fixture.expected.json")),
                ("continued_table_fixture", continued_path, load_reference("continued_table_fixture.expected.json")),
            ]
        )

        for case_name, source, expected in cases:
            document = parse_file(source, ParserConfig())
            issues.extend(compare_document(case_name, document, expected))
            print(
                f"[PASS] {case_name}: "
                f"pages={document.metadata.page_count}, "
                f"sections={document.metadata.section_count}, "
                f"tables={document.metadata.table_count}, "
                f"chunks={document.metadata.chunk_count}"
            )

    if issues:
        print("\nBenchmark issues detected:")
        for issue in issues:
            print(f"- {issue}")
        return 1

    print("\nAll benchmark cases passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
