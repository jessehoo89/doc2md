from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from zhdocparser.chunkers.basic import chunk_document
from zhdocparser.config import ParserConfig
from zhdocparser.exporters.chunk_exporter import export_chunks_json
from zhdocparser.exporters.json_exporter import export_document_json
from zhdocparser.exporters.markdown_exporter import export_document_markdown
from zhdocparser.extractors.factory import parse_document
from zhdocparser.schemas import Document

SUPPORTED_SUFFIXES = {".pdf", ".docx"}


@dataclass(slots=True)
class ExportBundle:
    directory: Path
    markdown_path: Path | None = None
    json_path: Path | None = None
    chunks_path: Path | None = None


def build_document(
    source: Path,
    *,
    config: ParserConfig | None = None,
) -> Document:
    active_config = config or ParserConfig()
    document = parse_document(source)
    if active_config.doc_type_override:
        document.metadata.doc_type = active_config.doc_type_override
    document.chunks = chunk_document(document, max_chars=active_config.max_chunk_chars)
    document.metadata.chunk_count = len(document.chunks)
    return document


def export_document_bundle(
    document: Document,
    source: Path,
    output_dir: Path,
    *,
    config: ParserConfig | None = None,
) -> ExportBundle:
    active_config = config or ParserConfig()
    bundle_dir = output_dir / safe_source_name(source)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle = ExportBundle(directory=bundle_dir)

    if active_config.export_markdown:
        bundle.markdown_path = bundle_dir / "document.md"
        export_document_markdown(document, bundle.markdown_path)

    if active_config.export_json:
        bundle.json_path = bundle_dir / "document.json"
        export_document_json(document, bundle.json_path)

    if active_config.export_chunks:
        bundle.chunks_path = bundle_dir / "chunks.json"
        export_chunks_json(document, bundle.chunks_path)

    return bundle


def iter_supported_files(source_dir: Path, *, recursive: bool = True) -> list[Path]:
    pattern = "**/*" if recursive else "*"
    files = [path for path in source_dir.glob(pattern) if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES]
    return sorted(files)


def safe_source_name(source: Path) -> str:
    return source.name.replace(".", "_")


def build_document_from_upload(
    filename: str,
    content: bytes,
    *,
    config: ParserConfig | None = None,
) -> Document:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported file type: {suffix}")

    with NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
        temp_path = Path(temp_file.name)
        temp_file.write(content)

    try:
        document = build_document(temp_path, config=config)
        document.metadata.source_file = filename
        document.metadata.source_name = Path(filename).name
        return document
    finally:
        temp_path.unlink(missing_ok=True)
