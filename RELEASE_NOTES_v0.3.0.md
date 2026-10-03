# ZhDocParser v0.3.0

ZhDocParser v0.3.0 focuses on making the project easier to integrate, easier to evaluate, and more credible as a reusable document-understanding component.

## Highlights

- Python SDK entry points: `parse_file`, `parse_dir`, `parse_and_export`
- Upload-based HTTP parsing with `/parse-upload`
- Selectable API response formats: `document`, `chunks`, `metadata`
- Continued-table merging across pages
- Weak-border text table detection
- Runnable `eval/` benchmark suite with reference expectations

## Quick Commands

```bash
python ./eval/run_benchmarks.py
zhdocparser parse ./samples/example.pdf -o ./outputs
zhdocparser serve --host 127.0.0.1 --port 8000
```

## Validation

- `pytest` passes
- SDK tests pass
- Upload API tests pass
- Evaluation script passes against all current reference cases

## Known Limits

- OCR and scanned-image understanding are still out of scope
- Table extraction still works best when column alignment is strong
- Multi-column recovery is heuristic-based and tuned for simple report layouts
