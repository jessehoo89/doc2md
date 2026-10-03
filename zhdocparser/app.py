from __future__ import annotations

from pathlib import Path

import typer

from zhdocparser.config import ParserConfig
from zhdocparser.service import build_document, export_document_bundle, iter_supported_files

app = typer.Typer(add_completion=False, help="Parse Chinese PDF and DOCX documents.")


@app.callback()
def main() -> None:
    """ZhDocParser command line interface."""


def _build_config(
    doc_type: str | None,
    max_chunk_chars: int,
    markdown: bool,
    json_output: bool,
    chunks: bool,
) -> ParserConfig:
    config = ParserConfig(
        doc_type_override=doc_type,
        max_chunk_chars=max_chunk_chars,
        export_markdown=markdown,
        export_json=json_output,
        export_chunks=chunks,
    )
    if config.has_any_export():
        return config
    raise typer.BadParameter("At least one output format must be enabled.")


def _print_bundle(bundle_dir: Path, markdown_path: Path | None, json_path: Path | None, chunks_path: Path | None) -> None:
    typer.echo(f"Bundle: {bundle_dir}")
    if markdown_path is not None:
        typer.echo(f"Markdown: {markdown_path}")
    if json_path is not None:
        typer.echo(f"JSON: {json_path}")
    if chunks_path is not None:
        typer.echo(f"Chunks: {chunks_path}")


def _print_summary(prefix: str, pages: int, sections: int, tables: int, chunks: int) -> None:
    typer.echo(
        f"{prefix}: "
        f"pages={pages}, "
        f"sections={sections}, "
        f"tables={tables}, "
        f"chunks={chunks}"
    )


@app.command("parse")
def parse_command(
    source: Path = typer.Argument(..., exists=True, readable=True, help="Source PDF or DOCX file."),
    output_dir: Path = typer.Option(Path("outputs"), "--output", "-o", help="Directory for exported files."),
    doc_type: str | None = typer.Option(None, "--doc-type", help="Override the inferred document type."),
    max_chunk_chars: int = typer.Option(500, "--max-chunk-chars", min=100, help="Maximum characters per chunk."),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Export Markdown output."),
    json_output: bool = typer.Option(True, "--json/--no-json", help="Export document JSON output."),
    chunks: bool = typer.Option(True, "--chunks/--no-chunks", help="Export standalone chunks JSON."),
    summary: bool = typer.Option(True, "--summary/--no-summary", help="Print a parsing summary after export."),
) -> None:
    """Parse a document and export structured outputs."""

    config = _build_config(doc_type, max_chunk_chars, markdown, json_output, chunks)
    document = build_document(source, config=config)
    bundle = export_document_bundle(document, source, output_dir, config=config)

    _print_bundle(bundle.directory, bundle.markdown_path, bundle.json_path, bundle.chunks_path)
    if summary:
        _print_summary(
            "Summary",
            document.metadata.page_count,
            len(document.sections),
            len(document.tables),
            len(document.chunks),
        )


@app.command("parse-dir")
def parse_dir_command(
    source_dir: Path = typer.Argument(..., exists=True, readable=True, dir_okay=True, file_okay=False, help="Directory containing PDF or DOCX files."),
    output_dir: Path = typer.Option(Path("outputs"), "--output", "-o", help="Directory for exported bundles."),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", help="Search source directory recursively."),
    doc_type: str | None = typer.Option(None, "--doc-type", help="Override the inferred document type for all files."),
    max_chunk_chars: int = typer.Option(500, "--max-chunk-chars", min=100, help="Maximum characters per chunk."),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Export Markdown output."),
    json_output: bool = typer.Option(True, "--json/--no-json", help="Export document JSON output."),
    chunks: bool = typer.Option(True, "--chunks/--no-chunks", help="Export standalone chunks JSON."),
    summary: bool = typer.Option(True, "--summary/--no-summary", help="Print a batch summary after export."),
) -> None:
    """Parse all supported files in a directory."""

    config = _build_config(doc_type, max_chunk_chars, markdown, json_output, chunks)
    files = iter_supported_files(source_dir, recursive=recursive)
    if not files:
        raise typer.BadParameter(f"No supported files found in: {source_dir}")

    success_count = 0
    for source in files:
        document = build_document(source, config=config)
        bundle = export_document_bundle(document, source, output_dir, config=config)
        _print_bundle(bundle.directory, bundle.markdown_path, bundle.json_path, bundle.chunks_path)
        success_count += 1

    if summary:
        typer.echo(f"Batch Summary: files={success_count}, output_dir={output_dir}")


@app.command("serve")
def serve_command(
    host: str = typer.Option("127.0.0.1", "--host", help="Host for the HTTP API."),
    port: int = typer.Option(8000, "--port", min=1, max=65535, help="Port for the HTTP API."),
) -> None:
    """Run the ZhDocParser HTTP API locally."""

    import uvicorn

    uvicorn.run("zhdocparser.api:app", host=host, port=port, reload=False)
