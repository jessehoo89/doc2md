# ZhDocParser v0.2.0

ZhDocParser v0.2.0 moves the project from a parser demo toward a usable document-ingestion tool for RAG and Agent pipelines.

## Highlights

- Better PDF reading order for simple multi-column report layouts
- Table context capture with `context_before` and `context_after`
- Standalone `chunks.json` export for RAG ingestion
- Batch parsing with `parse-dir`
- Minimal HTTP API with FastAPI
- Stronger metadata across `Document IR`, sections, tables, and chunks

## Quick Commands

```bash
zhdocparser parse ./samples/example.pdf -o ./outputs
zhdocparser parse-dir ./samples -o ./batch_outputs
zhdocparser serve --host 127.0.0.1 --port 8000
```

## Validation

- `pytest` passes
- CLI single-file parse passes
- CLI batch parse passes
- API parse test passes

## Known Limits

- No OCR fallback yet
- No scanned-PDF understanding yet
- PDF table extraction still works best with visible grid lines
- Merged-cell reconstruction is not solved in this version
