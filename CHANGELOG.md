# Changelog

## v0.3.0 - 2026-07-07

### Added

- Unified `ParserConfig` shared by CLI, SDK, and API
- Python SDK entry points for direct integration
- Upload-based API parsing with selectable response formats
- Runnable `eval/` benchmark suite with reference expectations
- Continued-table merging and weak-border text table detection

### Improved

- Chunk metadata now carries section paths, page ranges, and continued-table provenance
- Section metadata now exposes hierarchy via `parent_id` and `section_id_path`
- Parser service now supports both file-path and upload-driven flows

### Notes

- The current evaluation suite focuses on deterministic parser behavior, not OCR quality
- FastAPI tests still emit a `TestClient` deprecation warning from upstream dependencies

## v0.2.0 - 2026-07-07

### Added

- Batch parsing with `zhdocparser parse-dir`
- Standalone `chunks.json` export
- Minimal FastAPI service with `/health` and `/parse`
- Richer metadata for sections, tables, and chunks
- Regression tests for report-style reading order and table context binding
- CI coverage for the current parser and CLI surface

### Improved

- PDF reading order for simple two-column layouts
- Table title detection and surrounding context capture
- Document type inference for `government`, `education`, `report`, and `general`
- CLI output structure with per-document bundles

### Notes

- OCR and scanned-image understanding are still out of scope for this release
- Table extraction still relies on visible table borders in PDFs
