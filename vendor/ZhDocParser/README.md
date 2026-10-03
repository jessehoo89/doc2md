# ZhDocParser

ZhDocParser is a Chinese-first document parser for turning PDF and DOCX files into structured Markdown, JSON, and RAG-ready chunks.

It is not trying to be another OCR wrapper. The goal is higher in the stack: recover document structure, preserve table meaning, and emit outputs that are directly useful for knowledge bases, retrieval pipelines, and Agents.

## Why It Matters

Most document tools can extract text, but they still lose the structure that downstream AI systems need:

- Heading levels collapse into flat text
- Repeated headers and footers pollute retrieval
- Table rows lose context and become hard to use
- Chunking becomes blind and traceability disappears

ZhDocParser is built around a more useful contract:

- Parse Chinese PDF and DOCX into structured content
- Preserve section hierarchy and heading paths
- Keep table rows plus nearby context
- Export chunk metadata that can be fed into RAG directly

## What v0.3.0 Adds

- Better PDF reading order for simple multi-column reports
- Table context capture with `context_before` and `context_after`
- Standalone `chunks.json` export
- Batch parsing with `parse-dir`
- Minimal HTTP API with FastAPI
- Upload-based API parsing
- Python SDK entry points for direct integration
- Runnable `eval/` benchmark suite with reference expectations
- Richer `Document IR` metadata across documents, sections, tables, and chunks

## Quick Start

```bash
git clone https://github.com/melonelish/ZhDocParser.git
cd ZhDocParser
pip install -e .[dev]
python ./scripts/create_sample_documents.py
zhdocparser parse ./samples/example.pdf -o ./outputs
zhdocparser parse-dir ./samples -o ./batch_outputs
zhdocparser serve --host 127.0.0.1 --port 8000
```

Example CLI output:

```text
Bundle: outputs/example_pdf
Markdown: outputs/example_pdf/document.md
JSON: outputs/example_pdf/document.json
Chunks: outputs/example_pdf/chunks.json
Summary: pages=1, sections=3, tables=1, chunks=4
```

## Python SDK

```python
from zhdocparser import parse_file
from zhdocparser.config import ParserConfig

document = parse_file(
    "samples/example.docx",
    ParserConfig(doc_type_override="general", max_chunk_chars=500),
)

print(document.metadata.title)
print(len(document.chunks))
```

## Before / After

The most important improvements are easier to see than to describe.

Multi-column report reading order:

Before:

```text
Left column line 1
Right column line 1
Left column line 2
Right column line 2
```

After:

```text
Left column line 1
Left column line 2
Right column line 1
Right column line 2
```

Table context preservation:

Before:

```text
Rows only, no title or surrounding context.
```

After:

```json
{
  "title": "Table 1 Question Stats",
  "context_before": [
    "Question distribution is shown below.",
    "Table 1 Question Stats"
  ],
  "context_after": [
    "Post-table note"
  ]
}
```

More examples live in [examples/before_after.md](./examples/before_after.md) and [examples/README.md](./examples/README.md).

## Evaluation

Run the current benchmark suite:

```bash
python ./eval/run_benchmarks.py
```

The suite validates:

- generated sample PDF and DOCX parsing
- multi-column reading order
- weak-border text table detection
- continued-table merging across pages

## Features

- PDF parsing with heading recovery heuristics
- Better reading order for simple two-column pages
- Repeated header and footer filtering for multi-page PDFs
- DOCX parsing that preserves heading hierarchy and inline tables
- Structured `Document IR` with metadata, sections, tables, pages, and chunks
- Markdown export
- Full document JSON export
- Standalone `chunks.json` export
- CLI entrypoint for single-file and batch pipelines
- Minimal HTTP API for local integrations and file uploads
- Python SDK high-level interfaces: `parse_file`, `parse_dir`, `parse_and_export`

## HTTP API

Start the API locally:

```bash
zhdocparser serve --host 127.0.0.1 --port 8000
```

Example request:

```bash
curl -X POST http://127.0.0.1:8000/parse ^
  -H "Content-Type: application/json" ^
  -d "{\"source_path\":\"D:/fabuxiangmu2/samples/example.docx\",\"max_chunk_chars\":500}"
```

Upload a file directly:

```bash
curl -X POST http://127.0.0.1:8000/parse-upload ^
  -F "file=@D:/fabuxiangmu2/samples/example.docx" ^
  -F "response_format=metadata"
```

## Output Shape

```json
{
  "metadata": {
    "title": "中文复杂文档解析示例",
    "source_type": "pdf",
    "page_count": 1,
    "doc_type": "government",
    "source_name": "example.pdf",
    "section_count": 3,
    "table_count": 1
  },
  "pages": [],
  "sections": [],
  "tables": [],
  "chunks": []
}
```

## Benchmark

Current benchmark notes are in [benchmark.md](./benchmark.md). The current sample set covers:

- Chinese report-style PDF with headings and table content
- Chinese DOCX with heading hierarchy and inline tables
- Regression tests for report reading order and education-style table context
- Runnable reference-driven benchmark cases in [eval/README.md](./eval/README.md)

## Project Layout

```text
zhdocparser/
  chunkers/
  exporters/
  extractors/
  schemas/
  service.py
  api.py
examples/
scripts/
tests/
benchmark.md
eval/
CHANGELOG.md
RELEASE_NOTES_v0.2.0.md
RELEASE_NOTES_v0.3.0.md
```

## Testing

```bash
pytest
```

GitHub Actions CI is included in [`.github/workflows/ci.yml`](./.github/workflows/ci.yml).

## Current Scope

- Supported input: `PDF`, `DOCX`
- Supported output: `Markdown`, `JSON`, `chunks.json`
- Current strengths: headings, repeated header/footer cleanup, table context, chunk metadata, batch parsing
- Not included yet: OCR, scanned-image understanding, PPT/Excel, advanced merged-cell reconstruction

## Roadmap

- Better multi-column recovery beyond simple report layouts
- OCR fallback for scanned documents
- Stronger PDF table extraction without explicit grid lines
- Broader benchmark corpus for government, education, and enterprise documents
- MCP server and richer HTTP API surface

## Release Docs

- [CHANGELOG.md](./CHANGELOG.md)
- [RELEASE_NOTES_v0.2.0.md](./RELEASE_NOTES_v0.2.0.md)
- [RELEASE_NOTES_v0.3.0.md](./RELEASE_NOTES_v0.3.0.md)

## License

[MIT](./LICENSE)
