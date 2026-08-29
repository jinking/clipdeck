"""RED contracts for the Layer 3 MinerU ingestion boundary.

These tests deliberately exercise the public boundaries described in the Layer 3
specification.  They do not call the real MinerU service; the client is expected
to accept an ``httpx.MockTransport`` so the request shape and recovery rules can
be tested offline.
"""

from __future__ import annotations

import io
import json
import logging
import stat
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest


def _field(value: Any, name: str) -> Any:
    """Read a field from either a model/dataclass or a mapping.

    The tests intentionally do not force a serialization library on the
    implementation.  A small adapter keeps the contract about behavior rather
    than whether the implementation uses Pydantic, dataclasses, or dictionaries.
    """

    if isinstance(value, dict):
        return value[name]
    return getattr(value, name)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _symlink_zip_bytes(name: str, target: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo(name)
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, target.encode("utf-8"))
    return buffer.getvalue()


def _make_mineru_client(client_type: Any, settings_type: Any, transport: httpx.MockTransport, token: str) -> Any:
    """Construct the client while allowing settings to be a model or plain kwargs.

    The intended contract is ``MinerUSettings`` + ``MinerUClient``.  The fallback
    only keeps the RED test useful if the implementation chooses to expose the
    same settings as constructor kwargs.
    """

    base_url = "https://mineru.test"
    if settings_type is not None:
        try:
            settings = settings_type(base_url=base_url, token=token)
        except TypeError:
            settings = settings_type(base_url=base_url, api_token=token)
        try:
            return client_type(settings=settings, transport=transport)
        except TypeError:
            return client_type(config=settings, transport=transport)
    try:
        return client_type(base_url=base_url, token=token, transport=transport)
    except TypeError:
        return client_type(base_url=base_url, api_token=token, transport=transport)


def test_remote_state_mapping_is_explicit_and_unknown_states_are_not_success() -> None:
    from clipdeck.ingestion.providers.mineru.schemas import RemoteState, map_remote_state

    assert map_remote_state("waiting-file") is RemoteState.UPLOADING
    assert map_remote_state("pending") is RemoteState.SUBMITTED
    assert map_remote_state("running") is RemoteState.POLLING
    assert map_remote_state("converting") is RemoteState.POLLING
    assert map_remote_state("done") is RemoteState.DONE
    assert map_remote_state("failed") is RemoteState.FAILED

    with pytest.raises(ValueError, match="unknown|unsupported"):
        map_remote_state("provider-added-state")


@pytest.mark.asyncio
async def test_mineru_precise_client_posts_batch_uploads_without_auth_and_polls_result() -> None:
    from clipdeck.ingestion.providers.mineru.client import MinerUClient, MinerUSettings

    token = "test-mineru-token-do-not-log"
    uploaded: list[httpx.Request] = []
    requested: list[httpx.Request] = []
    result_zip = _zip_bytes({"full.md": "# 论文标题\n\n正文", "images/fig-1.png": b"png"})
    signed_upload_url = "https://upload.mineru.test/file?X-Amz-Signature=fixture-signature"
    signed_result_url = "https://download.mineru.test/result.zip?signature=fixture-signature"

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        if request.url.path == "/api/v4/file-urls/batch":
            assert request.headers["authorization"] == f"Bearer {token}"
            payload = json.loads(request.content)
            assert payload["files"] == [
                {"name": "asset-1.pdf", "data_id": "asset-1-fingerprint", "is_ocr": False}
            ]
            assert payload["model_version"] == "vlm"
            assert payload["enable_formula"] is True
            assert payload["enable_table"] is True
            assert payload["language"] == "ch"
            return httpx.Response(
                200,
                json={"code": 0, "data": {"batch_id": "batch-1", "file_urls": [signed_upload_url]}},
                request=request,
            )
        if request.url.host == "upload.mineru.test":
            uploaded.append(request)
            assert "authorization" not in request.headers
            assert "content-type" not in request.headers
            assert request.content == b"%PDF-1.7 fixture"
            return httpx.Response(200, request=request)
        if request.url.path == "/api/v4/extract-results/batch/batch-1":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"batch_id": "batch-1", "state": "pending"}},
                request=request,
            )
        if request.url.path == "/result.zip":
            assert request.url.host == "download.mineru.test"
            return httpx.Response(200, content=result_zip, request=request)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    client = _make_mineru_client(MinerUClient, MinerUSettings, transport, token)
    batch = await client.create_file_batch(
        files=[{"name": "asset-1.pdf", "data_id": "asset-1-fingerprint", "is_ocr": False}],
        model_version="vlm",
        enable_formula=True,
        enable_table=True,
        language="ch",
    )
    assert _field(batch, "batch_id") == "batch-1"
    await client.upload_file(_field(batch, "file_urls")[0], b"%PDF-1.7 fixture")

    status = await client.get_batch_result("batch-1")
    assert _field(status, "state") == "pending"
    archive = await client.download_result_zip(signed_result_url)
    assert archive == result_zip
    assert len(uploaded) == 1
    assert [request.method for request in requested] == ["POST", "PUT", "GET", "GET"]


@pytest.mark.asyncio
async def test_mineru_client_does_not_log_token_or_follow_untrusted_result_host(caplog: pytest.LogCaptureFixture) -> None:
    from clipdeck.ingestion.providers.mineru.client import MinerUClient, MinerUSettings

    token = "ultra-secret-mineru-token"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/file-urls/batch":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"batch_id": "batch-2", "file_urls": []}},
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    client = _make_mineru_client(MinerUClient, MinerUSettings, httpx.MockTransport(handler), token)
    with caplog.at_level(logging.DEBUG):
        await client.create_file_batch(files=[], model_version="vlm")
    assert token not in caplog.text

    with pytest.raises(ValueError, match="host|allowlist|https"):
        await client.download_result_zip("http://evil.example/result.zip?X-Amz-Signature=secret")


@pytest.mark.asyncio
async def test_mineru_upload_http_error_never_exposes_signed_query() -> None:
    from clipdeck.ingestion.providers.mineru.client import MinerUClient, MinerUHTTPError, MinerUSettings

    signed_url = "https://upload.mineru.test/file?X-Amz-Signature=must-remain-secret"
    transport = httpx.MockTransport(lambda request: httpx.Response(403, request=request))
    client = MinerUClient(
        settings=MinerUSettings(base_url="https://mineru.test", token="fixture-token"),
        transport=transport,
    )

    with pytest.raises(MinerUHTTPError) as captured:
        await client.upload_file(signed_url, b"fixture")
    assert "X-Amz-Signature" not in str(captured.value)
    assert "must-remain-secret" not in str(captured.value)


def test_safe_zip_extracts_result_and_rejects_traversal_absolute_and_symlink_entries(tmp_path: Path) -> None:
    from clipdeck.ingestion.storage.safe_zip import UnsafeArchiveError, safe_extract_zip

    destination = tmp_path / "safe"
    safe_extract_zip(
        _zip_bytes({"full.md": b"# Title", "content_list.json": b"[]", "images/fig.png": b"PNG"}),
        destination,
    )
    assert (destination / "full.md").read_text(encoding="utf-8") == "# Title"
    assert (destination / "images" / "fig.png").read_bytes() == b"PNG"

    for malicious in (
        _zip_bytes({"../escaped.txt": b"nope"}),
        _zip_bytes({"/absolute.txt": b"nope"}),
        _symlink_zip_bytes("images/link", "../../outside"),
    ):
        with pytest.raises(UnsafeArchiveError):
            safe_extract_zip(malicious, tmp_path / "rejected")
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path / "outside").exists()


def test_safe_zip_enforces_member_and_uncompressed_size_limits(tmp_path: Path) -> None:
    from clipdeck.ingestion.storage.safe_zip import UnsafeArchiveError, safe_extract_zip

    many_members = _zip_bytes({"a.txt": b"a", "b.txt": b"b"})
    with pytest.raises(UnsafeArchiveError, match="member|entry|limit"):
        safe_extract_zip(many_members, tmp_path / "many", max_members=1)

    oversized = _zip_bytes({"full.md": b"0123456789"})
    with pytest.raises(UnsafeArchiveError, match="size|limit|large"):
        safe_extract_zip(oversized, tmp_path / "large", max_uncompressed_bytes=5)


def test_external_processing_policy_blocks_disabled_or_sensitive_sources() -> None:
    from clipdeck.ingestion.policy import ExternalProcessingDenied, ensure_external_processing_allowed

    with pytest.raises(ExternalProcessingDenied):
        ensure_external_processing_allowed(external_processing_allowed=False, sensitive_source=False)
    with pytest.raises(ExternalProcessingDenied):
        ensure_external_processing_allowed(external_processing_allowed=True, sensitive_source=True)

    ensure_external_processing_allowed(external_processing_allowed=True, sensitive_source=False)


@pytest.mark.asyncio
async def test_sqlite_external_job_survives_restart_and_only_safe_fields_are_persisted(tmp_path: Path) -> None:
    from clipdeck.ingestion.repository import SQLiteIngestionRepository

    database = tmp_path / "ingestion.db"
    token = "token-must-not-be-stored"
    signed_url = "https://upload.mineru.test/file?X-Amz-Signature=must-not-be-stored"

    repository = SQLiteIngestionRepository(database)
    await repository.initialize()
    await repository.upsert_external_job(
        run_id="run-1",
        provider="mineru",
        api_version="v4",
        batch_id="batch-1",
        data_id="asset-1-fingerprint",
        remote_state="running",
        request_options={
            "model_version": "vlm",
            "language": "ch",
            "authorization": f"Bearer {token}",
            "upload_url": signed_url,
        },
    )
    loaded = await repository.get_external_job("run-1")
    assert _field(loaded, "batch_id") == "batch-1"
    assert _field(loaded, "remote_state") == "running"
    await repository.close()

    reopened = SQLiteIngestionRepository(database)
    await reopened.initialize()
    resumable = await reopened.list_resumable_jobs()
    assert len(resumable) == 1
    assert _field(resumable[0], "batch_id") == "batch-1"
    assert _field(resumable[0], "remote_state") == "running"
    await reopened.close()

    persisted = database.read_bytes()
    assert token.encode() not in persisted
    assert signed_url.encode() not in persisted


@pytest.mark.asyncio
async def test_sqlite_resume_excludes_terminal_jobs_and_keeps_remote_batch_id(tmp_path: Path) -> None:
    from clipdeck.ingestion.repository import SQLiteIngestionRepository

    repository = SQLiteIngestionRepository(tmp_path / "jobs.db")
    await repository.initialize()
    for run_id, batch_id, state in (
        ("run-pending", "batch-pending", "pending"),
        ("run-converting", "batch-converting", "converting"),
        ("run-done", "batch-done", "done"),
        ("run-failed", "batch-failed", "failed"),
    ):
        await repository.upsert_external_job(
            run_id=run_id,
            provider="mineru",
            api_version="v4",
            batch_id=batch_id,
            data_id=run_id,
            remote_state=state,
            request_options={"model_version": "vlm"},
        )

    resumable = await repository.list_resumable_jobs()
    assert {(_field(job, "run_id"), _field(job, "batch_id")) for job in resumable} == {
        ("run-pending", "batch-pending"),
        ("run-converting", "batch-converting"),
    }
    await repository.close()


@pytest.mark.asyncio
async def test_evidence_assembler_uses_full_markdown_and_tracks_archive_json_and_images(tmp_path: Path) -> None:
    from clipdeck.ingestion.application.evidence_assembler import EvidenceAssembler

    archive = _zip_bytes(
        {
            "full.md": "# 论文标题\n\n![图 1](images/fig-1.png)\n".encode("utf-8"),
            "content_list.json": b'[{"type":"text","page_idx":0}]',
            "middle.json": b'{"pages": 1}',
            "images/fig-1.png": b"PNG-FIXTURE",
        }
    )
    assembler = EvidenceAssembler(output_root=tmp_path)
    evidence = await assembler.assemble(
        evidence_id="evidence-1",
        asset_id="asset-1",
        raw_sha256="raw-sha256",
        source_name="paper.pdf",
        result_zip=archive,
        pipeline_fingerprint="pipeline-fingerprint",
    )

    package_path = Path(_field(evidence, "package_path"))
    assert (package_path / "content.md").read_text(encoding="utf-8").startswith("# 论文标题")
    assert (package_path / "assets" / "fig-1.png").read_bytes() == b"PNG-FIXTURE"
    assert "assets/fig-1.png" in (package_path / "content.md").read_text(encoding="utf-8")
    assert "images/fig-1.png" not in (package_path / "content.md").read_text(encoding="utf-8")
    metadata = (package_path / "meta.yaml").read_text(encoding="utf-8")
    assert "evidence-1" in metadata
    assert "asset-1" in metadata
    assert "raw-sha256" in metadata
    assert "pipeline-fingerprint" in metadata
    assert (package_path / "diagnostics" / "mineru-result.zip").exists()
    assert (package_path / "diagnostics" / "content_list.json").exists()


@pytest.mark.asyncio
async def test_evidence_assembler_extracts_academic_identifiers_into_yaml(tmp_path: Path) -> None:
    from clipdeck.ingestion.application.evidence_assembler import EvidenceAssembler

    archive = _zip_bytes(
        {
            "full.md": (
                "# Research Study\n\n"
                "DOI: 10.1038/s41586-024-0001-x\n"
                "ClinicalTrials: NCT01234567\n"
            ).encode("utf-8"),
            "content_list.json": b"[]",
        }
    )
    assembler = EvidenceAssembler(output_root=tmp_path)
    evidence = await assembler.assemble(
        evidence_id="evidence-identifiers-1",
        asset_id="asset-ident-1",
        raw_sha256="sha-123",
        source_name="trial.pdf",
        result_zip=archive,
        pipeline_fingerprint="fingerprint-1",
    )
    package_path = Path(_field(evidence, "package_path"))
    meta_content = (package_path / "meta.yaml").read_text(encoding="utf-8")
    assert "10.1038/s41586-024-0001-x" in meta_content
    assert "NCT01234567" in meta_content


@pytest.mark.asyncio
async def test_evidence_assembler_publish_failure_preserves_previous_package(tmp_path: Path, monkeypatch) -> None:
    from clipdeck.ingestion.application import evidence_assembler as module

    package = tmp_path / "evidence-atomic"
    package.mkdir()
    (package / "content.md").write_text("old published content")
    original_replace = module.os.replace

    def fail_new_publish(source, destination):
        if Path(destination) == package and ".tmp" in Path(source).name:
            raise OSError("publish failed")
        return original_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_new_publish)
    with pytest.raises(OSError, match="publish failed"):
        await module.EvidenceAssembler(output_root=tmp_path).assemble(
            evidence_id="evidence-atomic", asset_id="asset", raw_sha256="sha",
            source_name="paper.pdf", result_zip=_zip_bytes({"full.md": b"new content" * 10}),
            pipeline_fingerprint="fp",
        )
    assert (package / "content.md").read_text() == "old published content"
    monkeypatch.setattr(module.os, "replace", original_replace)
    retried = await module.EvidenceAssembler(output_root=tmp_path).assemble(
        evidence_id="evidence-atomic", asset_id="asset", raw_sha256="sha",
        source_name="paper.pdf", result_zip=_zip_bytes({"full.md": b"retry content " * 10}),
        pipeline_fingerprint="fp",
    )
    assert "retry content" in (Path(retried.package_path) / "content.md").read_text()


@pytest.mark.asyncio
async def test_canonicalization_failure_preserves_existing_package(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from clipdeck.ingestion.application import IngestionService
    from clipdeck.ingestion.domain.models import EvidenceDocument

    package = tmp_path / "published"
    package.mkdir()
    (package / "content.md").write_text("old content")
    (package / "meta.yaml").write_text("old meta")
    canonical = tmp_path / "canonical.blob"
    canonical.write_text("canonical content")

    class FailingBlobStore:
        calls = 0

        async def put_path(self, path, *, mime_type, role):
            self.calls += 1
            if self.calls == 2:
                raise OSError("canonicalization failed")
            return SimpleNamespace(
                storage_uri=str(canonical), blob_id="sha256:new", sha256="new",
                size_bytes=canonical.stat().st_size, mime_type=mime_type,
            )

    service = IngestionService(
        raw_repository=None, ingestion_repository=None, blob_store=FailingBlobStore(),
        evidence_root=tmp_path,
    )
    evidence = EvidenceDocument(
        evidence_id="published", asset_id="00000000-0000-0000-0000-000000000001",
        raw_sha256="raw", pipeline_fingerprint="fp", package_path=package,
    )
    with pytest.raises(OSError, match="canonicalization failed"):
        await service._canonicalize_assembled_evidence(evidence, raw_parent_blob_id="sha256:raw-parent")
    assert (package / "content.md").read_text() == "old content"
    assert (package / "meta.yaml").read_text() == "old meta"
    await service._canonicalize_assembled_evidence(evidence, raw_parent_blob_id="sha256:raw-parent")
    assert (package / "content.md").read_text() == "canonical content"


@pytest.mark.asyncio
async def test_canonicalized_mineru_artifacts_reference_source_archive_blob(tmp_path: Path) -> None:
    from clipdeck.acquisition.storage import LocalBlobStore
    from clipdeck.ingestion.application import IngestionService
    from clipdeck.ingestion.domain.models import EvidenceDocument

    package = tmp_path / "package"
    (package / "assets").mkdir(parents=True)
    (package / "diagnostics").mkdir()
    (package / "content.md").write_text("content")
    (package / "meta.yaml").write_text("meta")
    (package / "assets" / "figure.png").write_bytes(b"PNG")
    (package / "diagnostics" / "mineru-result.zip").write_bytes(b"ZIP")
    service = IngestionService(
        raw_repository=None, ingestion_repository=None, blob_store=LocalBlobStore(tmp_path / "blobs"),
        evidence_root=tmp_path,
    )
    evidence = EvidenceDocument(
        evidence_id="ev", asset_id="00000000-0000-0000-0000-000000000001",
        raw_sha256="raw", pipeline_fingerprint="fp", package_path=package,
    )

    await service._canonicalize_assembled_evidence(evidence, raw_parent_blob_id="sha256:raw-parent")

    assert evidence.source_archive_blob_id
    derived = [item for item in evidence.artifacts if item.logical_path != "diagnostics/mineru-result.zip"]
    archive = next(item for item in evidence.artifacts if item.logical_path == "diagnostics/mineru-result.zip")
    assert derived
    assert all(item.parent_blob_id == evidence.source_archive_blob_id for item in derived)
    assert archive.parent_blob_id == "sha256:raw-parent"
