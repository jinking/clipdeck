from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sci_radar.acquisition.domain import (
    AcquisitionInput,
    AcquisitionTask,
    AttemptStatus,
    BlobRef,
    BlobRole,
    FetchAttempt,
    ProviderFetchResult,
    RawAsset,
    ResourceClassifier,
    ResourceType,
    SourceKind,
    TaskStatus,
    ValidationStatus,
    ingestion_hint,
    utcnow,
)
from sci_radar.acquisition.providers import ProviderResolver
from sci_radar.acquisition.repository import SQLiteRepository
from sci_radar.acquisition.storage import LocalBlobStore


class AcquisitionService:
    def __init__(
        self,
        *,
        repository: SQLiteRepository,
        blob_store: LocalBlobStore,
        classifier: ResourceClassifier | None = None,
        resolver: ProviderResolver | None = None,
        max_attempts: int = 3,
    ):
        self.repository = repository
        self.blob_store = blob_store
        self.classifier = classifier or ResourceClassifier()
        self.resolver = resolver or ProviderResolver()
        self.max_attempts = max_attempts

    async def submit(self, request: AcquisitionInput) -> AcquisitionTask:
        staged_blob: BlobRef | None = None
        if request.source_kind is SourceKind.URL:
            normalized = self.classifier.normalize_url(request.url or "")
            resource_type = self.classifier.classify_url(normalized)
            source_key = request.source_key or normalized
        elif request.source_kind is SourceKind.TEXT:
            normalized = None
            resource_type = ResourceType.TEXT
            source_key = request.source_key or f"text:{uuid4()}"
            staged_blob = await self.blob_store.put(
                (request.text or "").encode("utf-8"), mime_type="text/plain; charset=utf-8",
                role=BlobRole.PASTED_TEXT,
            )
        else:
            normalized = None
            resource_type = self.classifier.classify_upload(request.filename, request.mime_type)
            source_key = request.source_key or f"upload:{uuid4()}"
            role = BlobRole.VIDEO if resource_type is ResourceType.VIDEO else (
                BlobRole.AUDIO if resource_type is ResourceType.PODCAST else BlobRole.SOURCE_FILE
            )
            staged_blob = request.staged_blob or await self.blob_store.put(
                request.data or b"", mime_type=request.mime_type, role=role,
            )

        provider = self.classifier.provider_for(resource_type, request.source_kind)
        resource_key = self.classifier.resource_key(resource_type, source_key)
        if not request.force_refetch:
            existing_asset = await self.repository.latest_asset(resource_key)
            if existing_asset is not None and existing_asset.acquisition_status in {TaskStatus.SUCCESS, TaskStatus.PARTIAL}:
                existing_task = await self.repository.get_task(existing_asset.task_id)
                if existing_task is not None and existing_task.status in {TaskStatus.SUCCESS, TaskStatus.PARTIAL}:
                    return existing_task

        task = AcquisitionTask(
            source_kind=request.source_kind,
            requested_url=request.url,
            normalized_transport_url=normalized,
            resource_type=resource_type,
            provider_name=provider,
            priority=request.priority,
            force_refetch=request.force_refetch,
            correlation_id=request.correlation_id,
            source_hint=request.source_hint,
            display_name=request.display_name or request.filename or request.url,
            source_key=source_key,
            capture_screenshot=request.capture_screenshot,
            staged_blob=staged_blob,
        )
        await self.repository.save_task(task)
        return task

    async def execute(self, task_id: UUID) -> RawAsset | None:
        task = await self.repository.get_task(task_id)
        if task is None:
            raise KeyError(f"Task {task_id} not found")
        if task.status in {TaskStatus.SUCCESS, TaskStatus.PARTIAL} and task.latest_asset_id:
            return await self.repository.get_asset(task.latest_asset_id)
        existing_asset = await self.repository.get_asset_for_task(task.task_id)
        if existing_asset is not None:
            task.latest_asset_id = existing_asset.asset_id
            task.status = existing_asset.acquisition_status
            task.finished_at = task.finished_at or existing_asset.fetched_at
            await self.repository.save_task(task)
            return existing_asset
        task.status = TaskStatus.RUNNING
        task.started_at = utcnow()
        await self.repository.save_task(task)

        for attempt_no in range(1, self.max_attempts + 1):
            attempt = FetchAttempt(
                task_id=task.task_id,
                attempt_no=attempt_no,
                provider_name=task.provider_name,
                requested_url=task.requested_url,
            )
            task.attempt_count = attempt_no
            await self.repository.save_attempt(attempt)

            started = datetime.now(UTC)
            if task.source_kind in {SourceKind.FILE, SourceKind.TEXT}:
                result = ProviderFetchResult(
                    success=True,
                    validation_status=ValidationStatus.VALID,
                    provider_meta={"transport": "local_input", "ingestion_hint": ingestion_hint(task.resource_type)},
                )
            else:
                provider = self.resolver.resolve(task.resource_type)
                result = await provider.fetch(task.normalized_transport_url or task.requested_url or "",
                                              capture_screenshot=task.capture_screenshot)

            attempt.finished_at = utcnow()
            attempt.duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
            attempt.final_url = result.final_url
            attempt.http_status = result.http_status
            attempt.response_headers = result.response_headers
            attempt.redirect_chain = result.redirect_chain
            attempt.validation_status = result.validation_status
            attempt.error_code = result.error_code
            attempt.error_message = result.error_message
            attempt.retryable = result.retryable

            if not result.success:
                if result.payloads:
                    debug = await self._store_payload(result.payloads[0])
                    attempt.debug_blob_id = debug.blob_id
                attempt.status = AttemptStatus.RETRYABLE_FAILURE if result.retryable else (
                    AttemptStatus.BLOCKED if result.validation_status is ValidationStatus.BLOCKED else AttemptStatus.PERMANENT_FAILURE
                )
                await self.repository.save_attempt(attempt)
                if result.retryable and attempt_no < self.max_attempts:
                    await asyncio.sleep(min(2 ** (attempt_no - 1), 4))
                    continue
                task.status = TaskStatus.BLOCKED if result.validation_status is ValidationStatus.BLOCKED else TaskStatus.FAILED
                task.finished_at = utcnow()
                task.last_error_code = result.error_code
                task.last_error_message = result.error_message
                await self.repository.save_task(task)
                return None

            attempt.status = AttemptStatus.SUCCESS
            await self.repository.save_attempt(attempt)
            asset = await self._assemble(task, attempt, result)
            try:
                await self.blob_store.materialize_asset_view(asset)
            except OSError as exc:
                asset.warnings.append(f"asset_view_failed:{type(exc).__name__}")
            task.latest_asset_id = asset.asset_id
            task.status = asset.acquisition_status
            task.finished_at = utcnow()
            await self.repository.complete_task_with_asset(task, asset)
            return asset
        return None

    async def _store_payload(self, payload) -> BlobRef:
        return await self.blob_store.put(
            payload.data, mime_type=payload.mime_type, role=payload.role, original_url=payload.original_url,
        )

    async def _assemble(self, task: AcquisitionTask, attempt: FetchAttempt, result: ProviderFetchResult) -> RawAsset:
        stored: list[BlobRef] = []
        child_assets: list[BlobRef] = []
        primary: BlobRef | None = task.staged_blob
        if task.staged_blob:
            stored.append(task.staged_blob)
        for payload in result.payloads:
            blob = await self._store_payload(payload)
            stored.append(blob)
            if payload.is_primary or primary is None:
                primary = blob
        for payload in result.child_payloads:
            child_assets.append(await self._store_payload(payload))
        if primary is None:
            raise RuntimeError("Successful provider result has no primary payload")

        resource_key = self.classifier.resource_key(task.resource_type, task.source_key)
        previous = await self.repository.latest_asset(resource_key)
        version_no = previous.version_no + 1 if previous else 1
        warnings = list(result.warnings)
        status = TaskStatus.PARTIAL if warnings and any("child_" in warning for warning in warnings) else TaskStatus.SUCCESS
        meta = dict(result.provider_meta)
        meta.setdefault("ingestion_hint", ingestion_hint(task.resource_type))
        meta.setdefault("source_kind", task.source_kind)
        return RawAsset(
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            resource_key=resource_key,
            version_no=version_no,
            resource_type=task.resource_type,
            provider_name=task.provider_name,
            requested_url=task.requested_url,
            final_url=result.final_url or task.normalized_transport_url,
            http_status=result.http_status,
            response_headers=result.response_headers,
            redirect_chain=result.redirect_chain,
            validation_status=result.validation_status,
            primary_blob=primary,
            blobs=stored,
            child_assets=child_assets,
            raw_sha256=primary.sha256,
            previous_asset_id=previous.asset_id if previous else None,
            changed_from_previous=(primary.sha256 != previous.raw_sha256) if previous else None,
            acquisition_status=status,
            warnings=warnings,
            provider_meta=meta,
        )

    async def refetch(self, asset_id: UUID) -> AcquisitionTask:
        asset = await self.repository.get_asset(asset_id)
        if not asset:
            raise KeyError(f"Asset {asset_id} not found")
        original = await self.repository.get_task(asset.task_id)
        if not original or original.source_kind is not SourceKind.URL:
            raise ValueError("Only URL assets can be refetched; upload a new file/text version explicitly")
        return await self.submit(AcquisitionInput(
            source_kind=SourceKind.URL, url=original.requested_url, source_key=original.source_key,
            display_name=original.display_name, force_refetch=True,
        ))
