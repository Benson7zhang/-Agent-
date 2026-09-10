from __future__ import annotations

import hashlib
import importlib
import ipaddress
import logging
import os
import re
from argparse import ArgumentParser
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit
from uuid import uuid4

from fastapi import FastAPI, File, Form, Query, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api_models import (
    ApplicationService,
    ConversationCreateRequest,
    ConversationResponse,
    ConversationTurnRequest,
    ConversationTurnResponse,
    DocumentKind,
    DocumentListResponse,
    DocumentRecord,
    DocumentResponse,
    ErrorDetail,
    ErrorEnvelope,
    FactDecisionRequest,
    FactDecisionResponse,
    FactListResponse,
    FactResponse,
    FactStatus,
    HealthResponse,
    JobCreateRequest,
    JobListResponse,
    JobResponse,
    JobRetryRequest,
    QueryRunResponse,
    ServiceError,
)
from .ingestion_state import SourceIdentityError, sha256_file

LOGGER = logging.getLogger(__name__)
API_PREFIX = "/api/v1"
PDF_SIGNATURE = b"%PDF-"
PDF_MIME_TYPE = "application/pdf"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
UPLOAD_CHUNK_SIZE = 1024 * 1024
FILE_READ_CHUNK_SIZE = 64 * 1024
MULTIPART_ENVELOPE_BYTES = 64 * 1024
RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")
CursorQuery = Annotated[str | None, Query(min_length=1, max_length=18, pattern=r"^\d+$")]
COMMON_ERROR_RESPONSES: dict[int, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Malformed request"},
    403: {"model": ErrorEnvelope, "description": "Origin is not allowed"},
    404: {"model": ErrorEnvelope, "description": "Resource not found"},
    409: {"model": ErrorEnvelope, "description": "Version or state conflict"},
    411: {"model": ErrorEnvelope, "description": "Upload Content-Length is required"},
    413: {"model": ErrorEnvelope, "description": "Upload request is too large"},
    422: {"model": ErrorEnvelope, "description": "Invalid request"},
    500: {"model": ErrorEnvelope, "description": "Unhandled server error"},
    503: {"model": ErrorEnvelope, "description": "Service unavailable"},
}


@dataclass(frozen=True, slots=True)
class ApiSettings:
    storage_root: Path
    host: str = "127.0.0.1"
    max_upload_bytes: int = 100 * 1024 * 1024
    cors_origins: tuple[str, ...] = ()


class ApiBoundaryError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers or {}


def create_app(service: ApplicationService, settings: ApiSettings) -> FastAPI:
    _validate_settings(settings)
    storage_root = settings.storage_root.resolve()
    documents_root = storage_root / "documents"
    documents_root.mkdir(parents=True, exist_ok=True)

    app = FastAPI(
        title="Smart FinQA API",
        version="1.0.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.application_service = service
    app.state.api_settings = settings
    app.state.storage_root = storage_root

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Accept", "Content-Type", "Range", "X-Request-ID"],
            expose_headers=["Accept-Ranges", "Content-Disposition", "Content-Length", "Content-Range", "X-Request-ID"],
        )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next: Any) -> Response:
        request_id = str(uuid4())
        request.state.request_id = request_id
        if _has_denied_origin(request, settings.cors_origins):
            return _error_response(
                request,
                status_code=status.HTTP_403_FORBIDDEN,
                code="cors_origin_forbidden",
                message="请求来源不在允许的跨域来源列表中",
            )
        upload_error = _upload_transport_error(request, settings.max_upload_bytes)
        if upload_error is not None:
            return _error_response(
                request,
                status_code=upload_error.status_code,
                code=upload_error.code,
                message=upload_error.message,
                details=upload_error.details,
                headers=upload_error.headers,
            )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(ServiceError)
    async def handle_service_error(request: Request, exc: ServiceError) -> JSONResponse:
        return _error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )

    @app.exception_handler(ApiBoundaryError)
    async def handle_boundary_error(request: Request, exc: ApiBoundaryError) -> JSONResponse:
        return _error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            request,
            status_code=422,
            code="request_validation_failed",
            message="请求参数校验失败",
            details=jsonable_encoder(exc.errors()),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "HTTP 请求失败"
        return _error_response(
            request,
            status_code=exc.status_code,
            code=f"http_{exc.status_code}",
            message=detail,
            details=None if isinstance(exc.detail, str) else exc.detail,
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception("Unhandled API error", extra={"request_id": _request_id(request)})
        return _error_response(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_server_error",
            message="服务发生未处理错误",
        )

    @app.get(
        f"{API_PREFIX}/health",
        response_model=HealthResponse,
        responses=COMMON_ERROR_RESPONSES,
        tags=["system"],
    )
    def health() -> HealthResponse:
        return HealthResponse()

    @app.post(
        f"{API_PREFIX}/documents",
        response_model=DocumentResponse,
        status_code=status.HTTP_201_CREATED,
        responses=COMMON_ERROR_RESPONSES,
        tags=["documents"],
    )
    async def upload_document(
        request: Request,
        file: Annotated[UploadFile, File()],
        kind: Annotated[DocumentKind, Form()],
    ) -> DocumentResponse:
        _reject_extra_form_fields(await request.form(), allowed={"file", "kind"})
        original_name = _validate_upload_metadata(file)
        document_id = str(uuid4())
        storage_key = PurePosixPath("documents", f"{document_id}.pdf").as_posix()
        stored_path = documents_root / f"{document_id}.pdf"
        digest, size_bytes = await _store_pdf(file, stored_path, settings.max_upload_bytes)
        record = DocumentRecord(
            document_id=document_id,
            kind=kind,
            original_name=original_name,
            storage_key=storage_key,
            sha256=digest,
            size_bytes=size_bytes,
            mime_type=PDF_MIME_TYPE,
            status="STORED",
            created_at=datetime.now(timezone.utc),
        )
        try:
            registered = await run_in_threadpool(service.register_document, record)
        except Exception:
            stored_path.unlink(missing_ok=True)
            raise
        return _as_document_record(registered).to_response()

    @app.get(
        f"{API_PREFIX}/documents",
        response_model=DocumentListResponse,
        responses=COMMON_ERROR_RESPONSES,
        tags=["documents"],
    )
    def list_documents(
        cursor: CursorQuery = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> DocumentListResponse:
        items, next_cursor, total = service.list_documents(cursor=cursor, limit=limit)
        return DocumentListResponse(
            items=[_as_document_response(item) for item in items],
            next_cursor=next_cursor,
            total=total,
        )

    @app.get(
        f"{API_PREFIX}/documents/{{document_id}}",
        response_model=DocumentResponse,
        responses=COMMON_ERROR_RESPONSES,
        tags=["documents"],
    )
    def get_document(document_id: str) -> DocumentResponse:
        return _as_document_record(service.get_document(document_id)).to_response()

    @app.get(
        f"{API_PREFIX}/documents/{{document_id}}/content",
        responses={
            **COMMON_ERROR_RESPONSES,
            200: {"content": {PDF_MIME_TYPE: {}}, "description": "Complete PDF"},
            206: {"content": {PDF_MIME_TYPE: {}}, "description": "Single PDF byte range"},
            416: {"model": ErrorEnvelope, "description": "Invalid or unsatisfiable byte range"},
        },
        tags=["documents"],
    )
    def get_document_content(document_id: str, request: Request) -> Response:
        record = _as_document_record(service.get_document(document_id))
        path = _resolve_stored_file(storage_root, record.storage_key)
        _require_document_content_match(path, record.sha256, record.size_bytes)
        size_bytes = path.stat().st_size
        range_header = request.headers.get("range")
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": _content_disposition("inline", record.original_name),
        }
        if range_header is None:
            headers["Content-Length"] = str(size_bytes)
            return StreamingResponse(
                _read_file_range(path, 0, size_bytes - 1),
                media_type=PDF_MIME_TYPE,
                headers=headers,
            )

        start, end = _parse_range(range_header, size_bytes)
        headers["Content-Length"] = str(end - start + 1)
        headers["Content-Range"] = f"bytes {start}-{end}/{size_bytes}"
        return StreamingResponse(
            _read_file_range(path, start, end),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            media_type=PDF_MIME_TYPE,
            headers=headers,
        )

    @app.post(
        f"{API_PREFIX}/jobs",
        response_model=JobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=COMMON_ERROR_RESPONSES,
        tags=["jobs"],
    )
    def create_job(request: JobCreateRequest) -> JobResponse:
        return JobResponse.model_validate(service.create_job(request))

    @app.get(
        f"{API_PREFIX}/jobs",
        response_model=JobListResponse,
        responses=COMMON_ERROR_RESPONSES,
        tags=["jobs"],
    )
    def list_jobs(
        cursor: CursorQuery = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> JobListResponse:
        items, next_cursor = service.list_jobs(cursor=cursor, limit=limit)
        return JobListResponse(
            items=[JobResponse.model_validate(item) for item in items],
            next_cursor=next_cursor,
        )

    @app.get(
        f"{API_PREFIX}/jobs/{{job_id}}",
        response_model=JobResponse,
        responses=COMMON_ERROR_RESPONSES,
        tags=["jobs"],
    )
    def get_job(job_id: str) -> JobResponse:
        return JobResponse.model_validate(service.get_job(job_id))

    @app.post(
        f"{API_PREFIX}/jobs/{{job_id}}/retry",
        response_model=JobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=COMMON_ERROR_RESPONSES,
        tags=["jobs"],
    )
    def retry_job(job_id: str, request: JobRetryRequest) -> JobResponse:
        return JobResponse.model_validate(service.retry_job(job_id, idempotency_key=request.idempotency_key))

    @app.post(
        f"{API_PREFIX}/conversations",
        response_model=ConversationResponse,
        status_code=status.HTTP_201_CREATED,
        responses=COMMON_ERROR_RESPONSES,
        tags=["conversations"],
    )
    def create_conversation(request: ConversationCreateRequest) -> ConversationResponse:
        return ConversationResponse.model_validate(service.create_conversation(request))

    @app.get(
        f"{API_PREFIX}/conversations/{{conversation_id}}",
        response_model=ConversationResponse,
        responses=COMMON_ERROR_RESPONSES,
        tags=["conversations"],
    )
    def get_conversation(conversation_id: str) -> ConversationResponse:
        return ConversationResponse.model_validate(service.get_conversation(conversation_id))

    @app.post(
        f"{API_PREFIX}/conversations/{{conversation_id}}/turns",
        response_model=ConversationTurnResponse,
        status_code=status.HTTP_201_CREATED,
        responses=COMMON_ERROR_RESPONSES,
        tags=["conversations"],
    )
    def create_turn(
        conversation_id: str,
        payload: ConversationTurnRequest,
        request: Request,
    ) -> ConversationTurnResponse:
        result = service.create_turn(conversation_id, payload, request_id=_request_id(request))
        return ConversationTurnResponse.model_validate(result)

    @app.get(
        f"{API_PREFIX}/facts",
        response_model=FactListResponse,
        responses=COMMON_ERROR_RESPONSES,
        tags=["facts"],
    )
    def list_facts(
        validation_status: Annotated[FactStatus | None, Query()] = None,
        company_id: Annotated[str | None, Query(max_length=200)] = None,
        period: Annotated[str | None, Query(max_length=20)] = None,
        cursor: CursorQuery = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> FactListResponse:
        items, next_cursor, total = service.list_facts(
            validation_status=validation_status,
            company_id=company_id,
            period=period,
            cursor=cursor,
            limit=limit,
        )
        return FactListResponse(
            items=[FactResponse.model_validate(item) for item in items],
            next_cursor=next_cursor,
            total=total,
        )

    @app.get(
        f"{API_PREFIX}/facts/{{fact_key}}",
        response_model=FactResponse,
        responses=COMMON_ERROR_RESPONSES,
        tags=["facts"],
    )
    def get_fact(fact_key: str) -> FactResponse:
        return FactResponse.model_validate(service.get_fact(fact_key))

    @app.post(
        f"{API_PREFIX}/facts/{{fact_key}}/decisions",
        response_model=FactDecisionResponse,
        responses=COMMON_ERROR_RESPONSES,
        tags=["facts"],
    )
    def decide_fact(fact_key: str, request: FactDecisionRequest) -> FactDecisionResponse:
        return FactDecisionResponse.model_validate(service.decide_fact(fact_key, request))

    @app.get(
        f"{API_PREFIX}/query-runs/{{query_run_id}}",
        response_model=QueryRunResponse,
        responses=COMMON_ERROR_RESPONSES,
        tags=["query-runs"],
    )
    def get_query_run(query_run_id: str) -> QueryRunResponse:
        return QueryRunResponse.model_validate(service.get_query_run(query_run_id))

    @app.get(
        f"{API_PREFIX}/query-runs/{{query_run_id}}/export.xlsx",
        responses={
            **COMMON_ERROR_RESPONSES,
            200: {"content": {XLSX_MIME_TYPE: {}}, "description": "Excel export"},
        },
        tags=["query-runs"],
    )
    def export_query_run(query_run_id: str) -> Response:
        content, filename = service.export_query_run(query_run_id)
        if not isinstance(content, bytes):
            raise TypeError("export_query_run must return bytes")
        return Response(
            content=content,
            media_type=XLSX_MIME_TYPE,
            headers={"Content-Disposition": _content_disposition("attachment", filename)},
        )

    return app


def main(argv: list[str] | None = None) -> None:
    parser = ArgumentParser(description="Run the Smart FinQA local web API")
    parser.add_argument(
        "--service-factory",
        default="smart_finqa.runtime:create_web_service",
        help="Trusted no-argument service factory in module:attribute form",
    )
    parser.add_argument("--base-dir", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--storage-root", type=Path, default=Path("outputs/web_storage"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--max-upload-bytes", type=int, default=100 * 1024 * 1024)
    parser.add_argument("--cors-origin", action="append", default=[])
    args = parser.parse_args(argv)

    resolved_base_dir = args.base_dir.resolve()
    resolved_storage_root = (
        args.storage_root.resolve()
        if args.storage_root.is_absolute()
        else (resolved_base_dir / args.storage_root).resolve()
    )
    os.environ["SMART_FINQA_BASE_DIR"] = str(resolved_base_dir)
    os.environ["SMART_FINQA_STORAGE_ROOT"] = str(resolved_storage_root)
    if args.config is not None:
        os.environ["SMART_FINQA_CONFIG"] = str(args.config.resolve())

    settings = ApiSettings(
        storage_root=resolved_storage_root,
        host=args.host,
        max_upload_bytes=args.max_upload_bytes,
        cors_origins=tuple(args.cors_origin),
    )
    _validate_settings(settings)
    service = _load_service(args.service_factory)
    app = create_app(service, settings)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


def _load_service(import_path: str) -> ApplicationService:
    module_name, separator, attribute_name = import_path.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("service factory must use module:attribute syntax")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute_name)
    if not callable(factory):
        raise TypeError("service factory must be callable")
    service = factory()
    if not isinstance(service, ApplicationService):
        raise TypeError("service factory result does not implement ApplicationService")
    return service


def _validate_settings(settings: ApiSettings) -> None:
    host = settings.host.strip().strip("[]")
    if host.lower() != "localhost":
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise ValueError("unauthenticated API host must be a loopback address")
    if settings.max_upload_bytes < len(PDF_SIGNATURE):
        raise ValueError("max_upload_bytes must be large enough for a PDF signature")
    for origin in settings.cors_origins:
        _validate_cors_origin(origin)


def _validate_cors_origin(origin: str) -> None:
    if origin == "*":
        raise ValueError("CORS wildcard origins are forbidden")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"invalid CORS origin: {origin!r}")


def _has_denied_origin(request: Request, allowed_origins: tuple[str, ...]) -> bool:
    if not request.url.path.startswith("/api/"):
        return False
    origins = [value.decode("latin-1") for name, value in request.scope.get("headers", []) if name.lower() == b"origin"]
    if not origins:
        return False
    return len(origins) != 1 or origins[0] not in allowed_origins


def _upload_transport_error(request: Request, max_upload_bytes: int) -> ApiBoundaryError | None:
    if request.method != "POST" or request.url.path != f"{API_PREFIX}/documents":
        return None

    raw_headers = request.scope.get("headers", [])
    content_lengths = [
        value.decode("latin-1").strip() for name, value in raw_headers if name.lower() == b"content-length"
    ]
    transfer_encodings = [
        value.decode("latin-1").strip() for name, value in raw_headers if name.lower() == b"transfer-encoding"
    ]
    if transfer_encodings and content_lengths:
        return ApiBoundaryError(
            status.HTTP_400_BAD_REQUEST,
            "conflicting_transfer_length",
            "上传请求不得同时包含 Content-Length 和 Transfer-Encoding",
        )
    if transfer_encodings:
        return ApiBoundaryError(
            status.HTTP_411_LENGTH_REQUIRED,
            "chunked_upload_forbidden",
            "上传请求不接受分块传输，必须提供 Content-Length",
        )
    if not content_lengths:
        return ApiBoundaryError(
            status.HTTP_411_LENGTH_REQUIRED,
            "content_length_required",
            "上传请求必须提供 Content-Length",
        )
    if len(content_lengths) != 1 or "," in content_lengths[0]:
        return ApiBoundaryError(
            status.HTTP_400_BAD_REQUEST,
            "conflicting_content_length",
            "上传请求必须且只能包含一个 Content-Length",
        )

    raw_length = content_lengths[0]
    if len(raw_length) > 20 or not raw_length.isascii() or not raw_length.isdecimal():
        return ApiBoundaryError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_content_length",
            "Content-Length 必须是十进制非负整数",
        )
    content_length = int(raw_length)
    if content_length <= 0:
        return ApiBoundaryError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_content_length",
            "Content-Length 必须大于零",
        )
    max_request_bytes = max_upload_bytes + MULTIPART_ENVELOPE_BYTES
    if content_length > max_request_bytes:
        return ApiBoundaryError(
            413,
            "upload_request_too_large",
            "上传请求超过传输层大小限制",
            {"max_request_bytes": max_request_bytes},
        )
    return None


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", str(uuid4()))


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    envelope = ErrorEnvelope(error=ErrorDetail(code=code, message=message, details=details, request_id=request_id))
    response_headers = dict(headers or {})
    response_headers["X-Request-ID"] = request_id
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(envelope),
        headers=response_headers,
    )


def _reject_extra_form_fields(form: Any, *, allowed: set[str]) -> None:
    unexpected = sorted(set(form.keys()) - allowed)
    if unexpected:
        raise ApiBoundaryError(
            422,
            "request_validation_failed",
            "上传表单包含不允许的字段",
            [
                {"loc": ["body", field], "msg": "Extra inputs are not permitted", "type": "extra_forbidden"}
                for field in unexpected
            ],
        )


def _validate_upload_metadata(file: UploadFile) -> str:
    filename = (file.filename or "").strip()
    if not filename:
        raise ApiBoundaryError(422, "missing_filename", "上传文件必须包含文件名")
    if any(ord(character) < 32 for character in filename):
        raise ApiBoundaryError(422, "invalid_filename", "文件名包含无效控制字符")
    if len(filename) > 255 or "/" in filename or "\\" in filename:
        raise ApiBoundaryError(422, "invalid_filename", "文件名不得包含路径且长度不能超过 255 个字符")
    original_name = filename
    if not original_name.lower().endswith(".pdf"):
        raise ApiBoundaryError(422, "invalid_file_extension", "仅支持 .pdf 文件")
    content_type = (file.content_type or "").split(";", maxsplit=1)[0].strip().lower()
    if content_type != PDF_MIME_TYPE:
        raise ApiBoundaryError(422, "invalid_mime_type", "上传文件的 MIME 类型必须为 application/pdf")
    return original_name


async def _store_pdf(file: UploadFile, path: Path, max_upload_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    try:
        first_chunk = await file.read(min(UPLOAD_CHUNK_SIZE, max_upload_bytes + 1))
        if not first_chunk.startswith(PDF_SIGNATURE):
            raise ApiBoundaryError(422, "invalid_pdf_signature", "文件不包含有效的 PDF 签名")
        with path.open("xb") as output:
            chunk = first_chunk
            while chunk:
                total += len(chunk)
                if total > max_upload_bytes:
                    raise ApiBoundaryError(
                        422,
                        "upload_too_large",
                        "上传文件超过大小限制",
                        {"max_upload_bytes": max_upload_bytes},
                    )
                digest.update(chunk)
                output.write(chunk)
                chunk = await file.read(UPLOAD_CHUNK_SIZE)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return digest.hexdigest(), total


def _as_document_record(value: Any) -> DocumentRecord:
    return DocumentRecord.model_validate(value)


def _as_document_response(value: Any) -> DocumentResponse:
    if isinstance(value, DocumentRecord):
        return value.to_response()
    return DocumentResponse.model_validate(value)


def _resolve_stored_file(storage_root: Path, storage_key: str) -> Path:
    if "\\" in storage_key:
        raise ApiBoundaryError(403, "document_path_forbidden", "文档存储位置不在允许目录内")
    key = PurePosixPath(storage_key)
    if key.is_absolute() or ".." in key.parts or not key.parts:
        raise ApiBoundaryError(403, "document_path_forbidden", "文档存储位置不在允许目录内")
    try:
        resolved = (storage_root / Path(*key.parts)).resolve(strict=True)
    except FileNotFoundError as exc:
        raise ApiBoundaryError(404, "document_content_not_found", "文档内容不存在") from exc
    if not resolved.is_relative_to(storage_root) or not resolved.is_file():
        raise ApiBoundaryError(403, "document_path_forbidden", "文档存储位置不在允许目录内")
    return resolved


def _require_document_content_match(path: Path, expected_sha256: str, expected_size: int) -> None:
    try:
        current_sha256 = sha256_file(path)
        current_size = path.stat().st_size
    except (OSError, SourceIdentityError) as exc:
        raise ApiBoundaryError(409, "source_content_mismatch", "PDF 内容与登记来源不一致") from exc
    if current_sha256 != expected_sha256 or current_size != expected_size:
        raise ApiBoundaryError(409, "source_content_mismatch", "PDF 内容与登记来源不一致")


def _parse_range(value: str, size_bytes: int) -> tuple[int, int]:
    match = RANGE_PATTERN.fullmatch(value.strip())
    if match is None or "," in value:
        raise ApiBoundaryError(
            416,
            "invalid_range",
            "仅支持单段 bytes Range 请求",
            headers={"Content-Range": f"bytes */{size_bytes}"},
        )
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise ApiBoundaryError(
            416,
            "invalid_range",
            "Range 起止位置不能为空",
            headers={"Content-Range": f"bytes */{size_bytes}"},
        )

    if not start_text:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ApiBoundaryError(
                416,
                "invalid_range",
                "Range 后缀长度必须大于零",
                headers={"Content-Range": f"bytes */{size_bytes}"},
            )
        start = max(0, size_bytes - suffix_length)
        return start, size_bytes - 1

    start = int(start_text)
    end = int(end_text) if end_text else size_bytes - 1
    if start >= size_bytes or end < start:
        raise ApiBoundaryError(
            416,
            "range_not_satisfiable",
            "请求范围超出文档长度",
            headers={"Content-Range": f"bytes */{size_bytes}"},
        )
    return start, min(end, size_bytes - 1)


def _read_file_range(path: Path, start: int, end: int) -> Iterator[bytes]:
    remaining = end - start + 1
    with path.open("rb") as source:
        source.seek(start)
        while remaining:
            chunk = source.read(min(FILE_READ_CHUNK_SIZE, remaining))
            if not chunk:
                raise OSError("document ended before the requested byte range")
            remaining -= len(chunk)
            yield chunk


def _content_disposition(disposition: Literal["inline", "attachment"], filename: str) -> str:
    encoded = quote(filename, safe="")
    return f"{disposition}; filename*=UTF-8''{encoded}"
