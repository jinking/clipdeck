from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Sequence
from uuid import UUID, uuid4

from clipdeck.acquisition.domain import (
    AcquisitionInput,
    AcquisitionTask,
    AttemptStatus,
    BlobRef,
    BlobRole,
    FetchAttempt,
    ProviderFetchResult,
    ProviderName,
    RawAsset,
    ResourceClassifier,
    ResourceType,
    SourceKind,
    TaskStatus,
    ValidationStatus,
    ingestion_hint,
    utcnow,
)
from clipdeck.acquisition.encoding import decode_html
from clipdeck.acquisition.providers import AcquisitionProvider, ProviderResolver, SpiderBypassProvider
from clipdeck.acquisition.repository import SQLiteRepository
from clipdeck.acquisition.storage import LocalBlobStore
from clipdeck.acquisition.truncation import TruncationDetector


_DEFAULT_SPIDER: Any = object()


class AcquisitionService:
    def __init__(
        self,
        *,
        repository: SQLiteRepository,
        blob_store: LocalBlobStore,
        classifier: ResourceClassifier | None = None,
        resolver: ProviderResolver | None = None,
        max_attempts: int = 3,
        max_concurrency: int = 5,
        login_provider: AcquisitionProvider | None = None,
        spider_provider: AcquisitionProvider | None = _DEFAULT_SPIDER,
        truncation_detector: TruncationDetector | None = None,
    ):
        self.repository = repository
        self.blob_store = blob_store
        self.classifier = classifier or ResourceClassifier()
        self.resolver = resolver or ProviderResolver()
        self.max_attempts = max_attempts
        self.max_concurrency = max_concurrency
        self.login_provider = login_provider
        self.spider_provider = SpiderBypassProvider() if spider_provider is _DEFAULT_SPIDER else spider_provider
        self.truncation_detector = truncation_detector
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))

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
        async with self._semaphore:
            return await self._execute_impl(task_id)

    async def _execute_impl(self, task_id: UUID) -> RawAsset | None:
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

            truncation = self._detect_truncation(task, result)
            if truncation is None:
                return await self._finalize(task, attempt, result)

            # The anonymous capture rendered fine but the server withheld the
            # remainder behind a login gate. Archive the truncated capture as
            # provenance, then try tiered upgrades (Spider bypass -> Login browser).
            result.provider_meta["truncation"] = truncation
            extra_warnings = [f"login_truncated:{truncation['marker']}"]
            target_url = result.final_url or task.normalized_transport_url or task.requested_url or ""

            upgraded_result: ProviderFetchResult | None = None
            upgraded_provider: ProviderName | None = None
            upgrade_warning: str | None = None
            current_attempt_no = attempt.attempt_no
            upgrade_attempt_no = current_attempt_no + 1

            # Tier 1: Try search engine spider bypass (zero-overhead HTTP fetch).
            if self.spider_provider is not None:
                current_attempt_no += 1
                spider_res, spider_err = await self._spider_upgrade(task, target_url)
                if spider_res is not None:
                    upgraded_result = spider_res
                    upgraded_provider = ProviderName.SPIDER_BYPASS
                    upgrade_warning = "spider_upgraded"
                    upgrade_attempt_no = current_attempt_no
                elif spider_err:
                    extra_warnings.append(spider_err)
                    await self.repository.save_attempt(
                        FetchAttempt(
                            task_id=task.task_id,
                            attempt_no=current_attempt_no,
                            provider_name=ProviderName.SPIDER_BYPASS,
                            requested_url=target_url,
                            final_url=target_url,
                            error_code=spider_err,
                            status=AttemptStatus.PERMANENT_FAILURE,
                            finished_at=utcnow(),
                        )
                    )

            # Tier 2: Try user's logged-in browser over CDP.
            if upgraded_result is None and self.login_provider is not None:
                current_attempt_no += 1
                login_res, login_err = await self._login_upgrade(task, target_url)
                if login_res is not None:
                    upgraded_result = login_res
                    upgraded_provider = ProviderName.LOGIN_BROWSER
                    upgrade_warning = "login_upgraded"
                    upgrade_attempt_no = current_attempt_no
                elif login_err:
                    extra_warnings.append(login_err)
                    await self.repository.save_attempt(
                        FetchAttempt(
                            task_id=task.task_id,
                            attempt_no=current_attempt_no,
                            provider_name=ProviderName.LOGIN_BROWSER,
                            requested_url=target_url,
                            final_url=target_url,
                            error_code=login_err,
                            status=AttemptStatus.PERMANENT_FAILURE,
                            finished_at=utcnow(),
                        )
                    )

            anonymous_asset = await self._finalize(task, attempt, result, extra_warnings=extra_warnings)
            if upgraded_result is None or upgraded_provider is None:
                return anonymous_asset

            upgrade_attempt = FetchAttempt(
                task_id=task.task_id,
                attempt_no=upgrade_attempt_no,
                provider_name=upgraded_provider,
                requested_url=attempt.requested_url,
                final_url=upgraded_result.final_url,
                http_status=upgraded_result.http_status,
                response_headers=upgraded_result.response_headers,
                redirect_chain=upgraded_result.redirect_chain,
                validation_status=upgraded_result.validation_status,
                status=AttemptStatus.SUCCESS,
                finished_at=utcnow(),
            )
            await self.repository.save_attempt(upgrade_attempt)
            return await self._finalize(
                task,
                upgrade_attempt,
                upgraded_result,
                provider_name=upgraded_provider,
                extra_warnings=[upgrade_warning] if upgrade_warning else [],
            )
        return None

    def _detect_truncation(
        self, task: AcquisitionTask, result: ProviderFetchResult
    ) -> dict | None:
        """Login/paywall truncation check for successful web-page captures."""
        if self.truncation_detector is None:
            return None
        if self.login_provider is None and self.spider_provider is None:
            return None
        if task.resource_type is not ResourceType.WEB_PAGE:
            return None
        primary = next((p for p in result.payloads if p.is_primary), None)
        if primary is None or not primary.data:
            return None
        html = decode_html(primary.data, primary.mime_type)
        url = result.final_url or task.normalized_transport_url or task.requested_url or ""
        return self.truncation_detector.detect(url, html)

    async def _spider_upgrade(
        self, task: AcquisitionTask, target_url: str
    ) -> tuple[ProviderFetchResult | None, str | None]:
        if self.spider_provider is None:
            return None, None
        try:
            upgraded = await self.spider_provider.fetch(target_url, capture_screenshot=task.capture_screenshot)
        except Exception as exc:
            return None, f"spider_upgrade_error:{type(exc).__name__}"
        if not upgraded.success:
            return None, f"spider_upgrade_failed:{upgraded.error_code or 'unknown'}"
        if self._detect_truncation(task, upgraded) is not None:
            return None, "spider_upgrade_still_truncated"
        return upgraded, None

    async def _login_upgrade(
        self, task: AcquisitionTask, target_url: str
    ) -> tuple[ProviderFetchResult | None, str | None]:
        if self.login_provider is None:
            return None, None
        try:
            upgraded = await self.login_provider.fetch(target_url, capture_screenshot=task.capture_screenshot)
        except Exception as exc:
            return None, f"login_upgrade_error:{type(exc).__name__}"
        if not upgraded.success:
            return None, f"login_upgrade_failed:{upgraded.error_code or 'unknown'}"
        if self._detect_truncation(task, upgraded) is not None:
            return None, "login_upgrade_still_truncated"
        return upgraded, None

    async def _finalize(
        self,
        task: AcquisitionTask,
        attempt: FetchAttempt,
        result: ProviderFetchResult,
        *,
        provider_name: ProviderName | None = None,
        extra_warnings: Sequence[str] = (),
    ) -> RawAsset:
        result.warnings.extend(extra_warnings)
        asset = await self._assemble(task, attempt, result, provider_name=provider_name)
        try:
            await self.blob_store.materialize_asset_view(asset)
        except OSError as exc:
            asset.warnings.append(f"asset_view_failed:{type(exc).__name__}")
        task.latest_asset_id = asset.asset_id
        task.status = asset.acquisition_status
        task.finished_at = utcnow()
        await self.repository.complete_task_with_asset(task, asset)
        return asset

    async def _store_payload(self, payload) -> BlobRef:
        return await self.blob_store.put(
            payload.data, mime_type=payload.mime_type, role=payload.role, original_url=payload.original_url,
        )

    async def _assemble(
        self,
        task: AcquisitionTask,
        attempt: FetchAttempt,
        result: ProviderFetchResult,
        *,
        provider_name: ProviderName | None = None,
    ) -> RawAsset:
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
            provider_name=provider_name or task.provider_name,
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
