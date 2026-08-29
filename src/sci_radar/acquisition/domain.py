from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class SourceKind(StrEnum):
    URL = "url"
    FILE = "file"
    TEXT = "text"


class ResourceType(StrEnum):
    WEB_PAGE = "web_page"
    WECHAT_ARTICLE = "wechat_article"
    PDF = "pdf"
    WORD = "word"
    TEXT = "text"
    VIDEO = "video"
    PODCAST = "podcast"
    BINARY_FILE = "binary_file"


class ProviderName(StrEnum):
    CRAWL4AI = "crawl4ai"
    WECHAT_ARTICLE = "wechat_article"
    DIRECT_DOWNLOAD = "direct_download"
    LOCAL_INPUT = "local_input"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class AttemptStatus(StrEnum):
    STARTED = "started"
    SUCCESS = "success"
    INVALID_CONTENT = "invalid_content"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"
    BLOCKED = "blocked"


class ValidationStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    DELETED = "deleted"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class BlobRole(StrEnum):
    PRIMARY_HTML = "primary_html"
    RAW_HTTP_BODY = "raw_http_body"
    RENDERED_HTML = "rendered_html"
    MHTML = "mhtml"
    SCREENSHOT = "screenshot"
    CHILD_IMAGE = "child_image"
    SOURCE_FILE = "source_file"
    PASTED_TEXT = "pasted_text"
    AUDIO = "audio"
    VIDEO = "video"
    DEBUG_RESPONSE = "debug_response"
    EVIDENCE_MARKDOWN = "evidence_markdown"
    EVIDENCE_YAML = "evidence_yaml"
    MINERU_RESULT_ARCHIVE = "mineru_result_archive"
    DERIVED_IMAGE = "derived_image"
    FIT_MARKDOWN = "fit_markdown"


class ErrorCode(StrEnum):
    INVALID_URL = "INVALID_URL"
    UNSUPPORTED_SCHEME = "UNSUPPORTED_SCHEME"
    DNS_ERROR = "DNS_ERROR"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    TIMEOUT = "TIMEOUT"
    HTTP_ERROR = "HTTP_ERROR"
    HTTP_403 = "HTTP_403"
    HTTP_404 = "HTTP_404"
    HTTP_429 = "HTTP_429"
    HTTP_5XX = "HTTP_5XX"
    WECHAT_DELETED = "WECHAT_DELETED"
    WECHAT_BLOCKED = "WECHAT_BLOCKED"
    WECHAT_INVALID_PAGE = "WECHAT_INVALID_PAGE"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    CONTENT_EMPTY_OR_BLOCKED = "CONTENT_EMPTY_OR_BLOCKED"
    UNSAFE_TARGET = "UNSAFE_TARGET"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    UNKNOWN = "UNKNOWN"


class AcquisitionInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    source_kind: SourceKind
    url: str | None = None
    text: str | None = None
    data: bytes | None = Field(default=None, exclude=True)
    staged_blob: Any | None = None
    filename: str | None = None
    mime_type: str | None = None
    display_name: str | None = None
    source_key: str | None = None
    priority: int = Field(default=50, ge=0, le=100)
    force_refetch: bool = False
    capture_screenshot: bool = False
    correlation_id: str | None = None
    source_hint: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> AcquisitionInput:
        if self.source_kind is SourceKind.URL and not self.url:
            raise ValueError("URL source requires url")
        if self.source_kind is SourceKind.TEXT and not self.text:
            raise ValueError("Text source requires non-empty text")
        if self.source_kind is SourceKind.FILE and self.data is None and self.staged_blob is None:
            raise ValueError("File source requires data or staged_blob")
        return self


class BlobRef(BaseModel):
    blob_id: str
    role: BlobRole
    sha256: str
    size_bytes: int
    mime_type: str | None = None
    storage_uri: str
    original_url: str | None = None
    fetched_at: datetime = Field(default_factory=utcnow)


class AcquisitionTask(BaseModel):
    task_id: UUID = Field(default_factory=uuid4)
    source_kind: SourceKind
    requested_url: str | None = None
    normalized_transport_url: str | None = None
    resource_type: ResourceType
    provider_name: ProviderName
    priority: int = 50
    force_refetch: bool = False
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempt_count: int = 0
    correlation_id: str | None = None
    source_hint: str | None = None
    display_name: str | None = None
    source_key: str
    capture_screenshot: bool = False
    staged_blob: BlobRef | None = None
    latest_asset_id: UUID | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None


class FetchAttempt(BaseModel):
    attempt_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    attempt_no: int
    provider_name: ProviderName
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    duration_ms: int | None = None
    requested_url: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    response_headers: dict[str, str] = Field(default_factory=dict)
    redirect_chain: list[str] = Field(default_factory=list)
    validation_status: ValidationStatus | None = None
    status: AttemptStatus = AttemptStatus.STARTED
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    debug_blob_id: str | None = None


class ProviderPayload(BaseModel):
    data: bytes = Field(exclude=True)
    role: BlobRole
    mime_type: str | None = None
    original_url: str | None = None
    is_primary: bool = False


class ProviderFetchResult(BaseModel):
    success: bool
    requested_url: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    response_headers: dict[str, str] = Field(default_factory=dict)
    redirect_chain: list[str] = Field(default_factory=list)
    validation_status: ValidationStatus = ValidationStatus.UNKNOWN
    payloads: list[ProviderPayload] = Field(default_factory=list)
    child_payloads: list[ProviderPayload] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    provider_meta: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False


class RawAsset(BaseModel):
    asset_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    attempt_id: UUID
    resource_key: str
    version_no: int
    resource_type: ResourceType
    provider_name: ProviderName
    requested_url: str | None = None
    final_url: str | None = None
    fetched_at: datetime = Field(default_factory=utcnow)
    http_status: int | None = None
    response_headers: dict[str, str] = Field(default_factory=dict)
    redirect_chain: list[str] = Field(default_factory=list)
    validation_status: ValidationStatus
    primary_blob: BlobRef
    blobs: list[BlobRef] = Field(default_factory=list)
    child_assets: list[BlobRef] = Field(default_factory=list)
    raw_sha256: str
    previous_asset_id: UUID | None = None
    changed_from_previous: bool | None = None
    acquisition_status: TaskStatus
    warnings: list[str] = Field(default_factory=list)
    provider_meta: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = 1


class ResourceClassifier:
    VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
    AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}
    WORD_EXTENSIONS = {".doc", ".docx", ".odt"}

    def normalize_url(self, url: str) -> str:
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ValueError("Only absolute HTTP(S) URLs are supported")
        scheme = parts.scheme.lower()
        host = parts.hostname.lower()
        port = parts.port
        if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
            host = f"{host}:{port}"
        return urlunsplit((scheme, host, parts.path or "/", parts.query, ""))

    def classify_url(self, url: str) -> ResourceType:
        parts = urlsplit(self.normalize_url(url))
        path = parts.path.lower()
        if parts.hostname == "mp.weixin.qq.com":
            return ResourceType.WECHAT_ARTICLE
        if path.endswith(".pdf"):
            return ResourceType.PDF
        if any(path.endswith(ext) for ext in self.VIDEO_EXTENSIONS):
            return ResourceType.VIDEO
        if any(path.endswith(ext) for ext in self.AUDIO_EXTENSIONS):
            return ResourceType.PODCAST
        if any(path.endswith(ext) for ext in self.WORD_EXTENSIONS):
            return ResourceType.WORD
        return ResourceType.WEB_PAGE

    def classify_upload(self, filename: str | None, mime_type: str | None) -> ResourceType:
        name = (filename or "").lower()
        mime = (mime_type or "").lower()
        if name.endswith(".pdf") or mime == "application/pdf":
            return ResourceType.PDF
        if any(name.endswith(ext) for ext in self.WORD_EXTENSIONS) or "wordprocessingml" in mime or mime == "application/msword":
            return ResourceType.WORD
        if mime.startswith("video/") or any(name.endswith(ext) for ext in self.VIDEO_EXTENSIONS):
            return ResourceType.VIDEO
        if mime.startswith("audio/") or any(name.endswith(ext) for ext in self.AUDIO_EXTENSIONS):
            return ResourceType.PODCAST
        if mime.startswith("text/") or name.endswith((".txt", ".md")):
            return ResourceType.TEXT
        return ResourceType.BINARY_FILE

    def classify_source(self, source_kind: SourceKind) -> ResourceType:
        if source_kind is SourceKind.TEXT:
            return ResourceType.TEXT
        raise ValueError(f"Source kind {source_kind} requires more context")

    def provider_for(self, resource_type: ResourceType, source_kind: SourceKind) -> ProviderName:
        if source_kind in {SourceKind.FILE, SourceKind.TEXT}:
            return ProviderName.LOCAL_INPUT
        if resource_type is ResourceType.WECHAT_ARTICLE:
            return ProviderName.WECHAT_ARTICLE
        if resource_type is ResourceType.WEB_PAGE:
            return ProviderName.CRAWL4AI
        return ProviderName.DIRECT_DOWNLOAD

    def resource_key(self, resource_type: ResourceType, source_key: str) -> str:
        return hashlib.sha256(f"{resource_type}:{source_key}".encode()).hexdigest()


def ingestion_hint(resource_type: ResourceType) -> str:
    if resource_type in {ResourceType.VIDEO, ResourceType.PODCAST}:
        return "media_transcription"
    if resource_type in {ResourceType.PDF, ResourceType.WORD}:
        return "document_text_extraction"
    if resource_type is ResourceType.TEXT:
        return "text_normalization"
    return "html_ingestion"
