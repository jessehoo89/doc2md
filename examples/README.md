# Example Outputs

These files are lightweight, text-only examples that show what ZhDocParser exports without requiring you to generate local outputs first.

## Files

- `example_pdf_output.md`: Markdown exported from the sample PDF
- `example_docx_output.md`: Markdown exported from the sample DOCX
- `chunk_metadata_example.json`: JSON snippet showing RAG chunk metadata
- `before_after.md`: text-based before/after comparisons for reading order and table context

## Regenerate Locally

```bash
python ./scripts/create_sample_documents.py
zhdocparser parse ./samples/example.pdf -o ./outputs
zhdocparser parse ./samples/example.docx -o ./outputs
```
