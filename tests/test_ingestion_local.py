"""RED contracts for local Layer 3 ingestion.

These tests deliberately describe the smallest public surface that the future
``sci_radar.ingestion`` package needs to expose.  They use already persisted
Layer 2 ``RawAsset`` records so the tests exercise the hand-off boundary rather
than re-testing acquisition providers.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from sci_radar.acquisition.domain import (
    AcquisitionTask,
    AttemptStatus,
    BlobRole,
    FetchAttempt,
    ProviderName,
    RawAsset,
    ResourceType,
    SourceKind,
    TaskStatus,
    ValidationStatus,
)
from sci_radar.acquisition.repository import SQLiteRepository
from sci_radar.acquisition.storage import LocalBlobStore
from sci_radar.ingestion.application import IngestionService
from sci_radar.ingestion.domain.models import IngestionOptions
from sci_radar.ingestion.repository import SQLiteIngestionRepository
from sci_radar.ingestion.queue import SingleIngestionWorker


@dataclass
class LocalIngestionContext:
    raw_repository: SQLiteRepository
    ingestion_repository: SQLiteIngestionRepository
    blob_store: LocalBlobStore
    service: IngestionService
    database_path: Path


@pytest.fixture
async def local_ingestion(tmp_path: Path):
    """Build an isolated Layer 2 + Layer 3 SQLite/local-Blob boundary."""

    raw_repository = SQLiteRepository(tmp_path / "acquisition.db")
    await raw_repository.initialize()
    ingestion_database = tmp_path / "ingestion.db"
    ingestion_repository = SQLiteIngestionRepository(ingestion_database)
    await ingestion_repository.initialize()
    blob_store = LocalBlobStore(tmp_path / "data")
    service = IngestionService(
        raw_repository=raw_repository,
        ingestion_repository=ingestion_repository,
        blob_store=blob_store,
        evidence_root=tmp_path / "evidence",
    )
    context = LocalIngestionContext(
        raw_repository=raw_repository,
        ingestion_repository=ingestion_repository,
        blob_store=blob_store,
        service=service,
        database_path=ingestion_database,
    )
    try:
        yield context
    finally:
        await ingestion_repository.close()
        await raw_repository.close()


async def _persist_raw_asset(
    context: LocalIngestionContext,
    *,
    resource_type: ResourceType,
    body: bytes,
    mime_type: str,
    role: BlobRole,
    requested_url: str | None = None,
    child_image: bytes | None = None,
) -> RawAsset:
    """Persist a realistic RawAsset using only Layer 2 public APIs."""

    task_id = uuid4()
    attempt_id = uuid4()
    is_local = requested_url is None
    source_kind = SourceKind.TEXT if resource_type is ResourceType.TEXT else SourceKind.URL
    provider_name = (
        ProviderName.LOCAL_INPUT
        if is_local
        else ProviderName.WECHAT_ARTICLE
        if resource_type is ResourceType.WECHAT_ARTICLE
        else ProviderName.CRAWL4AI
    )
    source_key = requested_url or f"local:{resource_type.value}:{uuid4()}"
    task = AcquisitionTask(
        task_id=task_id,
        source_kind=source_kind,
        requested_url=requested_url,
        normalized_transport_url=requested_url,
        resource_type=resource_type,
        provider_name=provider_name,
        source_key=source_key,
        display_name="本地测试素材",
        status=TaskStatus.SUCCESS,
        latest_asset_id=None,
    )
    await context.raw_repository.save_task(task)
    attempt = FetchAttempt(
        attempt_id=attempt_id,
        task_id=task_id,
        attempt_no=1,
        provider_name=provider_name,
        requested_url=requested_url,
        status=AttemptStatus.SUCCESS,
        validation_status=ValidationStatus.VALID,
    )
    await context.raw_repository.save_attempt(attempt)

    primary_blob = await context.blob_store.put(body, mime_type=mime_type, role=role, original_url=requested_url)
    child_assets = []
    if child_image is not None:
        child_assets.append(
            await context.blob_store.put(
                child_image,
                mime_type="image/png",
                role=BlobRole.CHILD_IMAGE,
                original_url="https://mmbiz.qpic.cn/example.png",
            )
        )
    asset = RawAsset(
        asset_id=uuid4(),
        task_id=task_id,
        attempt_id=attempt_id,
        resource_key=f"{resource_type.value}:{source_key}",
        version_no=1,
        resource_type=resource_type,
        provider_name=provider_name,
        requested_url=requested_url,
        final_url=requested_url,
        validation_status=ValidationStatus.VALID,
        primary_blob=primary_blob,
        blobs=[primary_blob],
        child_assets=child_assets,
        raw_sha256=primary_blob.sha256,
        acquisition_status=TaskStatus.SUCCESS,
        provider_meta={
            "ingestion_hint": "text_normalization" if resource_type is ResourceType.TEXT else "html_ingestion",
            "display_name": "本地测试素材",
        },
    )
    await context.raw_repository.save_asset(asset)
    await context.blob_store.materialize_asset_view(asset)
    return asset


def _view_path(evidence) -> Path:
    """The result contract exposes one stable path for the human-readable view."""

    view_uri = getattr(evidence, "view_uri", None)
    assert view_uri, "EvidenceDocument must expose view_uri"
    return Path(view_uri)


@pytest.mark.asyncio
async def test_text_raw_asset_becomes_local_evidence_without_semantic_rewrite(local_ingestion) -> None:
    source = "第一行\n\n第二行：保留原始语义。"
    asset = await _persist_raw_asset(
        local_ingestion,
        resource_type=ResourceType.TEXT,
        body=source.encode("utf-8"),
        mime_type="text/plain; charset=utf-8",
        role=BlobRole.PASTED_TEXT,
    )

    evidence = await local_ingestion.service.ingest(asset.asset_id, pipeline_fingerprint="local-text-v1")

    assert evidence.asset_id == asset.asset_id
    assert evidence.status == "success"
    view = _view_path(evidence)
    assert (view / "content.md").read_text(encoding="utf-8") == source
    meta = (view / "meta.yaml").read_text(encoding="utf-8")
    assert str(asset.asset_id) in meta
    assert asset.raw_sha256 in meta
    assert (view / "content.md").stat().st_ino == Path(evidence.markdown_blob_uri).stat().st_ino


@pytest.mark.asyncio
async def test_web_and_wechat_raw_html_are_converted_from_local_blob_only(local_ingestion) -> None:
    html = "<html><body><article><h1>本地网页标题</h1><p>网页正文。</p></article></body></html>"
    asset = await _persist_raw_asset(
        local_ingestion,
        resource_type=ResourceType.WEB_PAGE,
        body=html.encode("utf-8"),
        mime_type="text/html; charset=utf-8",
        role=BlobRole.RENDERED_HTML,
        requested_url="https://example.org/local-page",
    )

    evidence = await local_ingestion.service.ingest(asset.asset_id, pipeline_fingerprint="local-html-v1")

    markdown = (_view_path(evidence) / "content.md").read_text(encoding="utf-8")
    assert "本地网页标题" in markdown
    assert "网页正文" in markdown
    assert "example.org/local-page" in (
        (_view_path(evidence) / "meta.yaml").read_text(encoding="utf-8")
    )

    wechat_asset = await _persist_raw_asset(
        local_ingestion,
        resource_type=ResourceType.WECHAT_ARTICLE,
        body=(
            '<div id="js_article"><div id="js_content">'
            "<h2>微信文章</h2><p>微信正文。</p>"
            '<img data-src="https://mmbiz.qpic.cn/example.png">'
            "</div></div>"
        ).encode("utf-8"),
        mime_type="text/html; charset=utf-8",
        role=BlobRole.PRIMARY_HTML,
        requested_url="https://mp.weixin.qq.com/s/local",
        child_image=b"wechat-image-bytes",
    )

    wechat_evidence = await local_ingestion.service.ingest(
        wechat_asset.asset_id,
        pipeline_fingerprint="local-wechat-v1",
    )
    wechat_view = _view_path(wechat_evidence)
    wechat_markdown = (wechat_view / "content.md").read_text(encoding="utf-8")
    assert "微信文章" in wechat_markdown
    assert "微信正文" in wechat_markdown
    image_files = sorted((wechat_view / "assets").glob("*"))
    assert image_files and image_files[0].read_bytes() == b"wechat-image-bytes"
    assert image_files[0].stat().st_ino == Path(wechat_asset.child_assets[0].storage_uri).stat().st_ino
    assert image_files[0].stat().st_mode & 0o222 == 0


@pytest.mark.asyncio
async def test_local_llm_egress_follows_ingestion_options(local_ingestion) -> None:
    class RecordingExtractor:
        calls: list[str | None] = []

        async def extract(self, html, *, url=None):
            self.calls.append(url)
            return True, "# EXTERNAL LLM MARKDOWN\n\n" + ("authorized content " * 5)

    extractor = RecordingExtractor()
    local_ingestion.service.llm_extractor = extractor
    html = b"<html><body><article><h1>Local title</h1><p>Local body content for extraction.</p></article></body></html>"

    default_asset = await _persist_raw_asset(
        local_ingestion, resource_type=ResourceType.WEB_PAGE, body=html,
        mime_type="text/html", role=BlobRole.RENDERED_HTML,
        requested_url="https://example.org/default",
    )
    default = await local_ingestion.service.ingest(default_asset.asset_id, pipeline_fingerprint="llm-default")
    assert "EXTERNAL LLM" not in (_view_path(default) / "content.md").read_text()

    sensitive_asset = await _persist_raw_asset(
        local_ingestion, resource_type=ResourceType.WEB_PAGE, body=html,
        mime_type="text/html", role=BlobRole.RENDERED_HTML,
        requested_url="https://example.org/sensitive",
    )
    sensitive = await local_ingestion.service.ingest(
        sensitive_asset.asset_id, pipeline_fingerprint="llm-sensitive",
        options=IngestionOptions(external_processing_allowed=True, sensitive_source=True),
    )
    assert "EXTERNAL LLM" not in (_view_path(sensitive) / "content.md").read_text()

    allowed_asset = await _persist_raw_asset(
        local_ingestion, resource_type=ResourceType.WEB_PAGE, body=html,
        mime_type="text/html", role=BlobRole.RENDERED_HTML,
        requested_url="https://example.org/allowed",
    )
    allowed = await local_ingestion.service.ingest(
        allowed_asset.asset_id, pipeline_fingerprint="llm-allowed",
        options=IngestionOptions(external_processing_allowed=True, sensitive_source=False),
    )
    assert "EXTERNAL LLM" in (_view_path(allowed) / "content.md").read_text()
    assert extractor.calls == ["https://example.org/allowed"]


@pytest.mark.asyncio
async def test_same_web_asset_local_and_llm_variants_are_distinct_in_both_orders(local_ingestion) -> None:
    class VariantExtractor:
        provider_name = "fixture-llm"
        model = "fixture-model-v1"
        base_url = "https://llm.example/v1"

        async def extract(self, html, *, url=None):
            return True, "# LLM VARIANT\n\n" + ("external content " * 5)

    local_ingestion.service.llm_extractor = VariantExtractor()
    html = b"<article><h1>LOCAL VARIANT</h1><p>local source body with enough text for conversion</p></article>"

    local_first_asset = await _persist_raw_asset(
        local_ingestion, resource_type=ResourceType.WEB_PAGE, body=html,
        mime_type="text/html", role=BlobRole.RENDERED_HTML,
        requested_url="https://example.org/local-first",
    )
    local_first = await local_ingestion.service.ingest(local_first_asset.asset_id)
    llm_second = await local_ingestion.service.ingest(
        local_first_asset.asset_id,
        options=IngestionOptions(external_processing_allowed=True),
    )
    assert local_first.evidence_id != llm_second.evidence_id
    assert "LOCAL VARIANT" in (_view_path(local_first) / "content.md").read_text()
    assert "LLM VARIANT" in (_view_path(llm_second) / "content.md").read_text()
    local_meta = (_view_path(local_first) / "meta.yaml").read_text()
    llm_meta = (_view_path(llm_second) / "meta.yaml").read_text()
    assert 'provider: "local"' in local_meta
    assert 'fallback_status: "not_requested"' in local_meta
    assert 'provider: "fixture-llm"' in llm_meta
    assert 'model: "fixture-model-v1"' in llm_meta
    assert 'fallback_status: "not_needed"' in llm_meta

    llm_first_asset = await _persist_raw_asset(
        local_ingestion, resource_type=ResourceType.WEB_PAGE, body=html,
        mime_type="text/html", role=BlobRole.RENDERED_HTML,
        requested_url="https://example.org/llm-first",
    )
    llm_first = await local_ingestion.service.ingest(
        llm_first_asset.asset_id,
        options=IngestionOptions(external_processing_allowed=True),
    )
    local_second = await local_ingestion.service.ingest(llm_first_asset.asset_id)
    assert llm_first.evidence_id != local_second.evidence_id
    assert "LLM VARIANT" in (_view_path(llm_first) / "content.md").read_text()
    assert "LOCAL VARIANT" in (_view_path(local_second) / "content.md").read_text()


@pytest.mark.asyncio
async def test_authorized_llm_failure_records_local_fallback_metadata(local_ingestion) -> None:
    class FailingExtractor:
        provider_name = "fixture-llm"
        model = "fixture-model-v2"
        base_url = "https://llm.example/v1"

        async def extract(self, html, *, url=None):
            return False, ""

    local_ingestion.service.llm_extractor = FailingExtractor()
    asset = await _persist_raw_asset(
        local_ingestion, resource_type=ResourceType.WEB_PAGE,
        body=b"<article><h1>Fallback title</h1><p>Fallback local body text is retained.</p></article>",
        mime_type="text/html", role=BlobRole.RENDERED_HTML,
        requested_url="https://example.org/fallback",
    )
    evidence = await local_ingestion.service.ingest(
        asset.asset_id, options=IngestionOptions(external_processing_allowed=True),
    )
    meta = (_view_path(evidence) / "meta.yaml").read_text()
    assert 'provider: "local"' in meta
    assert 'requested_provider: "fixture-llm"' in meta
    assert 'model: "fixture-model-v2"' in meta
    assert 'fallback_status: "llm_failed"' in meta


@pytest.mark.asyncio
async def test_worker_close_completes_current_and_queued_submit_futures(local_ingestion) -> None:
    from sci_radar.ingestion.queue.worker import WorkerClosedError

    entered = __import__("asyncio").Event()
    never = __import__("asyncio").Event()

    class BlockingService:
        async def ingest(self, asset_id, *, pipeline_fingerprint, options):
            entered.set()
            await never.wait()

    worker = SingleIngestionWorker(
        BlockingService(), local_ingestion.ingestion_repository, close_timeout_seconds=0.01,
    )
    await worker.start()
    first = __import__("asyncio").create_task(worker.submit(uuid4()))
    await entered.wait()
    second = __import__("asyncio").create_task(worker.submit(uuid4()))
    await __import__("asyncio").sleep(0)
    await worker.close()

    results = await __import__("asyncio").gather(first, second, return_exceptions=True)
    assert all(isinstance(item, WorkerClosedError) for item in results)


@pytest.mark.asyncio
async def test_worker_recovery_rejects_changed_pipeline_variant(local_ingestion) -> None:
    class PendingExtractor:
        provider_name = "fixture-llm"
        model = "model-before-restart"
        base_url = "https://llm.example/v1"

    asset = await _persist_raw_asset(
        local_ingestion,
        resource_type=ResourceType.WEB_PAGE,
        body=b"<article><h1>Pending LLM</h1><p>body with enough local text to convert</p></article>",
        mime_type="text/html",
        role=BlobRole.RENDERED_HTML,
        requested_url="https://example.org/pending-llm",
    )
    options = IngestionOptions(external_processing_allowed=True)
    local_ingestion.service.llm_extractor = PendingExtractor()
    variant = local_ingestion.service.pipeline_variant_for(asset, options)
    fingerprint = local_ingestion.service.pipeline_fingerprint_for(asset, options)
    run = await local_ingestion.ingestion_repository.create_run(
        asset_id=asset.asset_id,
        resource_type=asset.resource_type,
        pipeline_fingerprint=fingerprint,
        converter_name=variant["converter"],
        execution_options=options.model_dump(),
        status="pending",
    )

    local_ingestion.service.llm_extractor = None
    worker = SingleIngestionWorker(local_ingestion.service, local_ingestion.ingestion_repository)
    await worker.start()
    await worker.queue.join()
    await worker.close()

    recovered = await local_ingestion.ingestion_repository.get_run(run.run_id)
    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.last_error_code == "PIPELINE_VARIANT_UNAVAILABLE"
    assert await local_ingestion.ingestion_repository.get_evidence_for_asset(
        asset.asset_id, fingerprint,
    ) is None


@pytest.mark.asyncio
async def test_same_asset_and_pipeline_fingerprint_is_idempotent(local_ingestion) -> None:
    asset = await _persist_raw_asset(
        local_ingestion,
        resource_type=ResourceType.TEXT,
        body=b"idempotent source",
        mime_type="text/plain",
        role=BlobRole.PASTED_TEXT,
    )

    first = await local_ingestion.service.ingest(asset.asset_id, pipeline_fingerprint="text-v1")
    second = await local_ingestion.service.ingest(asset.asset_id, pipeline_fingerprint="text-v1")

    assert first.evidence_id == second.evidence_id
    with sqlite3.connect(local_ingestion.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM evidence_documents").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_local_evidence_retry_atomically_replaces_failed_partial_view(local_ingestion, monkeypatch) -> None:
    asset = await _persist_raw_asset(
        local_ingestion,
        resource_type=ResourceType.TEXT,
        body=b"source",
        mime_type="text/plain",
        role=BlobRole.PASTED_TEXT,
    )
    import sci_radar.ingestion.application.service as service_module

    original = service_module._hardlink
    calls = 0

    def fail_after_markdown(source_uri, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated publish failure")
        return original(source_uri, target)

    monkeypatch.setattr(service_module, "_hardlink", fail_after_markdown)
    with pytest.raises(OSError, match="publish failure"):
        await local_ingestion.service._write_evidence(
            asset, run_id="failed", fingerprint="atomic-v1", markdown="old markdown\n"
        )

    monkeypatch.setattr(service_module, "_hardlink", original)
    evidence = await local_ingestion.service._write_evidence(
        asset, run_id="retry", fingerprint="atomic-v1", markdown="new markdown\n"
    )
    view = Path(evidence.view_uri)
    assert (view / "content.md").read_text() == "new markdown\n"
    assert (view / "content.md").stat().st_ino == Path(evidence.markdown_blob_uri).stat().st_ino
    assert not list(view.parent.glob(f".{view.name}.staging-*"))


@pytest.mark.asyncio
async def test_sqlite_single_worker_claim_and_stale_recovery(local_ingestion) -> None:
    run = await local_ingestion.ingestion_repository.create_run(
        asset_id=uuid4(),
        resource_type=ResourceType.TEXT,
        pipeline_fingerprint="text-v1",
        status="pending",
    )
    claimed = await local_ingestion.ingestion_repository.claim_next_run(worker_id="worker-1")
    assert claimed is not None
    assert claimed.run_id == run.run_id
    assert claimed.status == "running"
    assert await local_ingestion.ingestion_repository.claim_next_run(worker_id="worker-2") is None

    await local_ingestion.ingestion_repository.mark_stale_running(
        timeout_seconds=0,
        recovered_status="pending",
    )
    recovered = await local_ingestion.ingestion_repository.claim_next_run(worker_id="worker-2")
    assert recovered is not None
    assert recovered.run_id == run.run_id
    assert recovered.attempt_count == 2


@pytest.mark.asyncio
async def test_ingestion_database_contains_references_not_entity_bytes(local_ingestion) -> None:
    marker = b"unique-raw-body-that-must-not-be-in-sqlite"
    asset = await _persist_raw_asset(
        local_ingestion,
        resource_type=ResourceType.TEXT,
        body=marker,
        mime_type="text/plain",
        role=BlobRole.PASTED_TEXT,
    )
    evidence = await local_ingestion.service.ingest(asset.asset_id, pipeline_fingerprint="text-v1")
    assert _view_path(evidence).is_dir()

    raw_db = sqlite3.connect(local_ingestion.database_path)
    try:
        marker_text = marker.decode()
        table_names = raw_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (table_name,) in table_names:
            rows = raw_db.execute(f'SELECT * FROM "{table_name}"').fetchall()
            assert marker_text not in repr(rows)
            columns = raw_db.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            assert not any(
                str(column[1]).lower() in {"content", "body", "data", "bytes", "raw_bytes"}
                for column in columns
            )
    finally:
        raw_db.close()


@pytest.mark.asyncio
async def test_single_worker_recovers_a_pending_sqlite_run(local_ingestion) -> None:
    asset = await _persist_raw_asset(
        local_ingestion,
        resource_type=ResourceType.TEXT,
        body=b"recoverable",
        mime_type="text/plain",
        role=BlobRole.PASTED_TEXT,
    )
    run = await local_ingestion.ingestion_repository.create_run(
        asset_id=asset.asset_id,
        resource_type=asset.resource_type,
        pipeline_fingerprint="recover-v1",
        status="pending",
    )
    worker = SingleIngestionWorker(local_ingestion.service, local_ingestion.ingestion_repository)
    await worker.start()
    await worker.queue.join()
    await worker.close()

    recovered = await local_ingestion.ingestion_repository.get_run(run.run_id)
    assert recovered is not None
    assert recovered.status == "success"
    assert recovered.evidence_id


@pytest.mark.asyncio
async def test_single_worker_recovers_document_options_before_external_job(local_ingestion) -> None:
    asset = await _persist_raw_asset(
        local_ingestion,
        resource_type=ResourceType.PDF,
        body=b"%PDF-1.7 recovery",
        mime_type="application/pdf",
        role=BlobRole.SOURCE_FILE,
    )
    options = IngestionOptions(
        external_processing_allowed=True,
        model_version="pipeline",
        language="en",
        is_ocr=True,
    )
    await local_ingestion.ingestion_repository.create_run(
        asset_id=asset.asset_id,
        resource_type=asset.resource_type,
        pipeline_fingerprint="recover-document-v1",
        execution_options=options.model_dump(),
        status="pending",
    )

    class CapturingService:
        recovered: IngestionOptions | None = None

        async def ingest(self, asset_id, *, pipeline_fingerprint, options):
            assert asset_id == asset.asset_id
            assert pipeline_fingerprint == "recover-document-v1"
            self.recovered = options
            return None

    capturing = CapturingService()
    worker = SingleIngestionWorker(capturing, local_ingestion.ingestion_repository)
    await worker.start()
    await worker.queue.join()
    await worker.close()

    assert capturing.recovered == options
