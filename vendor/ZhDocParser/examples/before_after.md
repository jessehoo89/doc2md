# Before / After

This page shows why ZhDocParser is more useful than plain text extraction for downstream AI workflows.

## Case 1: Multi-column Report Reading Order

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

## Case 2: Table Context Preservation

Before:

```text
Table rows extracted without nearby explanation or title context.
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

## Case 3: RAG-ready Chunk Metadata

Before:

```text
Only raw text is available, with no section path or source traceability.
```

After:

```json
{
  "page_number": 1,
  "heading_path": ["中文复杂文档解析示例"],
  "chunk_type": "section",
  "metadata": {
    "source_name": "example.docx",
    "doc_type": "general",
    "section_heading": "中文复杂文档解析示例",
    "heading_path_text": "中文复杂文档解析示例"
  }
}
```
