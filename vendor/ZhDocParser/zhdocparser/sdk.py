from __future__ import annotations

from pathlib import Path

from zhdocparser.config import ParserConfig
from zhdocparser.schemas import Document
from zhdocparser.service import build_document, export_document_bundle, iter_supported_files


def parse_file(source: str | Path, config: ParserConfig | None = None) -> Document:
    source_path = Path(source)
    return build_document(source_path, config=config)


def parse_dir(source_dir: str | Path, config: ParserConfig | None = None, *, recursive: bool = True) -> list[Document]:
    source_dir_path = Path(source_dir)
    active_config = config or ParserConfig()
    return [build_document(path, config=active_config) for path in iter_supported_files(source_dir_path, recursive=recursive)]


def parse_and_export(source: str | Path, output_dir: str | Path, config: ParserConfig | None = None):
    source_path = Path(source)
    output_path = Path(output_dir)
    active_config = config or ParserConfig()
    document = build_document(source_path, config=active_config)
    bundle = export_document_bundle(document, source_path, output_path, config=active_config)
    return document, bundle
