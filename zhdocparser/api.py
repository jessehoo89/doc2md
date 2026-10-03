from __future__ import annotations

from enum import Enum
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from zhdocparser.config import ParserConfig
from zhdocparser.service import build_document, build_document_from_upload

app = FastAPI(title="ZhDocParser API", version="0.3.0")


class ResponseFormat(str, Enum):
    document = "document"
    chunks = "chunks"
    metadata = "metadata"


class ErrorResponse(BaseModel):
    detail: str
    error_code: str


class ParseRequest(BaseModel):
    source_path: str = Field(..., description="Absolute or relative path to a local PDF or DOCX file.")
    doc_type_override: str | None = None
    max_chunk_chars: int = Field(default=500, ge=100, le=5000)
    response_format: ResponseFormat = ResponseFormat.document


def _config_from_values(doc_type_override: str | None, max_chunk_chars: int) -> ParserConfig:
    return ParserConfig(doc_type_override=doc_type_override, max_chunk_chars=max_chunk_chars)


def _format_payload(document, response_format: ResponseFormat) -> dict | list | str:
    if response_format == ResponseFormat.chunks:
        return [chunk.model_dump() for chunk in document.chunks]
    if response_format == ResponseFormat.metadata:
        return document.metadata.model_dump()
    return document.model_dump()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/parse", responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
def parse_document_endpoint(request: ParseRequest) -> dict | list:
    source = Path(request.source_path)
    if not source.exists() or not source.is_file():
        raise HTTPException(status_code=404, detail={"detail": f"Source file not found: {source}", "error_code": "SOURCE_NOT_FOUND"})

    try:
        document = build_document(
            source,
            config=_config_from_values(request.doc_type_override, request.max_chunk_chars),
        )
    except Exception as exc:  # pragma: no cover - defensive API boundary
        raise HTTPException(
            status_code=400,
            detail={"detail": str(exc), "error_code": "PARSE_FAILED"},
        ) from exc

    return _format_payload(document, request.response_format)


@app.post("/parse-upload", responses={400: {"model": ErrorResponse}})
async def parse_upload_endpoint(
    file: UploadFile = File(...),
    doc_type_override: str | None = Form(default=None),
    max_chunk_chars: int = Form(default=500),
    response_format: ResponseFormat = Form(default=ResponseFormat.document),
) -> dict | list:
    if not file.filename:
        raise HTTPException(status_code=400, detail={"detail": "Missing uploaded filename.", "error_code": "MISSING_FILENAME"})

    try:
        content = await file.read()
        document = build_document_from_upload(
            file.filename,
            content,
            config=_config_from_values(doc_type_override, max_chunk_chars),
        )
    except Exception as exc:  # pragma: no cover - defensive API boundary
        raise HTTPException(
            status_code=400,
            detail={"detail": str(exc), "error_code": "UPLOAD_PARSE_FAILED"},
        ) from exc

    return _format_payload(document, response_format)
