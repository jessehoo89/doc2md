from __future__ import annotations

import json

from docx import Document as DocxDocument
from fastapi.testclient import TestClient

from zhdocparser.api import app


def test_api_parse_returns_document_payload(tmp_path) -> None:
    source = tmp_path / "demo.docx"
    doc = DocxDocument()
    doc.add_heading("Notice", level=1)
    doc.add_paragraph("API body content")
    doc.save(source)

    client = TestClient(app)
    response = client.post(
        "/parse",
        json={
            "source_path": str(source),
            "doc_type_override": "government",
            "max_chunk_chars": 120,
            "response_format": "document",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata"]["title"] == "Notice"
    assert payload["metadata"]["doc_type"] == "government"
    assert len(payload["chunks"]) == 1


def test_api_parse_supports_response_format_chunks(tmp_path) -> None:
    source = tmp_path / "demo.docx"
    doc = DocxDocument()
    doc.add_heading("Notice", level=1)
    doc.add_paragraph("Chunk body")
    doc.save(source)

    client = TestClient(app)
    response = client.post(
        "/parse",
        json={
            "source_path": str(source),
            "response_format": "chunks",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert payload[0]["chunk_type"] == "section"


def test_api_parse_upload_supports_file_upload_and_metadata_format(tmp_path) -> None:
    source = tmp_path / "upload.docx"
    doc = DocxDocument()
    doc.add_heading("Upload Notice", level=1)
    doc.add_paragraph("Uploaded content")
    doc.save(source)

    client = TestClient(app)
    response = client.post(
        "/parse-upload",
        data={
            "doc_type_override": "government",
            "max_chunk_chars": "120",
            "response_format": "metadata",
        },
        files={
            "file": (
                "upload.docx",
                source.read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "Upload Notice"
    assert payload["doc_type"] == "government"
    assert payload["chunk_count"] == 1


def test_api_parse_returns_structured_error_for_missing_file() -> None:
    client = TestClient(app)
    response = client.post(
        "/parse",
        json={
            "source_path": "missing.docx",
            "response_format": "document",
        },
    )

    assert response.status_code == 404
    payload = response.json()
    assert payload["detail"]["error_code"] == "SOURCE_NOT_FOUND"
