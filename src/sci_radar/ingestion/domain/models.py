from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from sci_radar.acquisition.domain import ResourceType, utcnow


class IngestionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PREPARING = "preparing"
    UPLOADING = "uploading"
    SUBMITTED = "submitted"
    POLLING = "polling"
    DOWNLOADING = "downloading"
    ASSEMBLING = "assembling"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    QUARANTINED = "quarantined"


class IngestionOptions(BaseModel):
    model_version: str = "vlm"
    language: str = "ch"
    enable_formula: bool = True
    enable_table: bool = True
    is_ocr: bool = False
    external_processing_allowed: bool = False
    sensitive_source: bool = False
    page_ranges: str | None = None


class IngestionRun(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    asset_id: UUID
    resource_type: ResourceType
    pipeline_version: str = "1.1.0"
    pipeline_fingerprint: str
    converter_name: str
    converter_version: str = "1.0.0"
    execution_options: dict[str, Any] = Field(default_factory=dict)
    status: IngestionStatus = IngestionStatus.PENDING
    attempt_count: int = 0
    evidence_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
    last_error_code: str | None = None
    last_error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ExternalParseJob(BaseModel):
    run_id: str
    provider: str
    api_version: str
    batch_id: str
    data_id: str
    remote_state: str
    request_options: dict[str, Any] = Field(default_factory=dict)
    poll_count: int = 0
    result_archive_blob_id: str | None = None
    provider_error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class DerivedArtifact(BaseModel):
    role: str
    blob_id: str | None = None
    parent_blob_id: str | None = None
    sha256: str
    mime_type: str | None = None
    size_bytes: int
    logical_path: str
    producer: str
    producer_version: str = "1.0.0"


class EvidenceDocument(BaseModel):
    evidence_id: str
    run_id: str | None = None
    asset_id: UUID | str
    raw_sha256: str
    pipeline_fingerprint: str
    markdown_blob_id: str | None = None
    yaml_blob_id: str | None = None
    source_archive_blob_id: str | None = None
    package_path: Path
    view_uri: str | None = None
    markdown_blob_uri: str | None = None
    status: IngestionStatus = IngestionStatus.SUCCESS
    warnings: list[str] = Field(default_factory=list)
    artifacts: list[DerivedArtifact] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
