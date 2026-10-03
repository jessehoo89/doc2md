# Benchmark Notes

ZhDocParser v0.3.0 focuses on release-quality parsing for Chinese PDF and DOCX documents with outputs that are directly useful for Markdown review, JSON ingestion, and RAG chunk pipelines.

## Sample Set

| Sample | Type | Focus | Expected Outcome |
| --- | --- | --- | --- |
| `example.pdf` | Chinese PDF | Heading recovery, repeated header/footer filtering, table extraction | Ordered Markdown sections, one extracted table, chunk metadata |
| `example.docx` | Chinese DOCX | Heading hierarchy, inline table extraction | Structured section tree, one extracted table, chunk metadata |
| `report.pdf` test fixture | Report-style PDF | Two-column reading order | Left column before right column |
| `education.pdf` test fixture | Education-style PDF | Table title and surrounding context | Table rows plus `context_before/context_after` |

## What We Validate

- Title recovery from the first meaningful heading
- Section hierarchy and heading path retention
- Repeated header/footer removal for multi-page PDFs
- Better reading order for simple two-column report layouts
- Table extraction into structured rows
- Table title capture and surrounding context binding
- RAG-ready chunks with source metadata

## Current Observed Results

| Case | Pages | Sections | Tables | Chunks | Key Result |
| --- | --- | --- | --- | --- | --- |
| `example.pdf` | 1 | 3 | 1 | 4 | Heading recovery, table extraction, chunk metadata |
| `example.docx` | 1 | 3 | 1 | 4 | Heading hierarchy and inline table extraction |
| `report.pdf` fixture | 1 | 1 | 0 | 1 | Multi-column reading order fixed |
| `education.pdf` fixture | 1 | 1 | 1 | 2 | Table context bound before and after |
| `continued_table_fixture.pdf` | 2 | 2 | 1 | 3 | Table merged across pages |
| `text_table_fixture.pdf` | 1 | 1 | 1 | 2 | Weak-border text table detected |

## Before / After Signals

### Multi-column ordering

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

### Table context

Before:

```text
Only row values survive.
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

## Current Limits

- PDF table extraction still relies on visible table lines and cell-box text reads
- OCR for scanned images is not included yet
- Multi-column recovery is heuristic-based and currently tuned for simple report-style layouts
- Complex merged cells are preserved as flat rows for now

## Runnable Eval

The repository now includes a runnable evaluation suite in [eval/README.md](./eval/README.md).

Run it with:

```bash
python ./eval/run_benchmarks.py
```

## Next Benchmark Expansion

- Government notice PDFs with repeated headers on every page
- Education materials with question/answer/analysis blocks
- Report PDFs with wider tables and appendix sections
- Real scanned PDFs after OCR fallback is introduced
