from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from clipdeck.acquisition.domain import AcquisitionInput, SourceKind
from clipdeck.acquisition.main import create_app
from clipdeck.ingestion.providers.mineru.schemas import BatchResult, FileBatch


@pytest.mark.asyncio
async def test_text_acquisition_automatically_creates_browsable_evidence(tmp_path: Path) -> None:
    app = create_app(data_root=tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/acquisitions/text",
                json={"text": "# 本地笔记\n\n正文", "display_name": "笔记"},
            )
            assert response.status_code == 201

            evidence_list = await client.get("/api/v1/evidence")
            assert evidence_list.status_code == 200
            evidence = evidence_list.json()[0]
            content = await client.get(f"/api/v1/evidence/{evidence['evidence_id']}/content")
            metadata = await client.get(f"/api/v1/evidence/{evidence['evidence_id']}/meta")
            package = await client.get(f"/api/v1/evidence/{evidence['evidence_id']}/package")

            assert content.text == "# 本地笔记\n\n正文"
            assert evidence["asset_id"] in metadata.text
            assert package.headers["content-type"] == "application/zip"


@pytest.mark.asyncio
async def test_ingestion_api_uses_service_pipeline_variant_fingerprint(tmp_path: Path, monkeypatch) -> None:
    app = create_app(data_root=tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            acquisition = await client.post(
                "/api/v1/acquisitions/text",
                json={"text": "variant-aware ingestion", "display_name": "variant"},
            )
            task_id = acquisition.json()["task_id"]
            asset_id = (await client.get(f"/api/v1/acquisitions/{task_id}")).json()["latest_asset_id"]

            monkeypatch.setattr(
                app.state.ingestion_service,
                "pipeline_fingerprint_for",
                lambda asset, options: "variant-specific-fingerprint",
                raising=False,
            )
            monkeypatch.setattr(
                app.state.ingestion_service,
                "pipeline_variant_for",
                lambda asset, options: {
                    "provider": "fixture-llm",
                    "converter": "text_fixture_llm",
                    "model": "fixture-model",
                },
                raising=False,
            )
            response = await client.post("/api/v1/ingestions", json={"asset_id": asset_id})

            assert response.status_code == 202
            assert response.json()["pipeline_fingerprint"] == "variant-specific-fingerprint"
            assert response.json()["converter_name"] == "text_fixture_llm"


@pytest.mark.asyncio
async def test_app_restart_recovers_persisted_pending_acquisition(tmp_path: Path) -> None:
    first = create_app(data_root=tmp_path)
    async with first.router.lifespan_context(first):
        task = await first.state.service.submit(AcquisitionInput(
            source_kind=SourceKind.TEXT,
            text="durable acquisition",
            display_name="recovery fixture",
        ))

    second = create_app(data_root=tmp_path)
    async with second.router.lifespan_context(second):
        for _ in range(100):
            recovered = await second.state.service.repository.get_task(task.task_id)
            if recovered and recovered.status == "success":
                break
            await asyncio.sleep(0.01)
        assert recovered is not None
        assert recovered.status == "success"
        assert recovered.latest_asset_id is not None


@pytest.mark.asyncio
async def test_document_ingestion_requires_permission_and_configured_mineru(tmp_path: Path) -> None:
    app = create_app(data_root=tmp_path, mineru_token="")
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            upload = await client.post(
                "/api/v1/acquisitions/file",
                files={"file": ("paper.pdf", b"%PDF-1.7 fixture", "application/pdf")},
            )
            task = (await client.get(f"/api/v1/acquisitions/{upload.json()['task_id']}")).json()
            asset_id = task["latest_asset_id"]

            denied = await client.post("/api/v1/ingestions", json={"asset_id": asset_id})
            unavailable = await client.post(
                "/api/v1/ingestions",
                json={"asset_id": asset_id, "external_processing_allowed": True},
            )
            assert denied.status_code == 409
            assert unavailable.status_code == 503
            assert "Token" not in unavailable.text


class _FakeMinerUClient:
    def __init__(self, archive: bytes):
        self.archive = archive
        self.uploaded = b""

    async def create_file_batch(self, **kwargs):
        assert kwargs["model_version"] == "vlm"
        return FileBatch(batch_id="batch-fixture", file_urls=["https://upload.example/signed"])

    async def upload_file(self, url: str, data: bytes) -> None:
        assert url.startswith("https://")
        self.uploaded = data

    async def get_batch_result(self, batch_id: str) -> BatchResult:
        return BatchResult(
            batch_id=batch_id,
            state="done",
            full_zip_url="https://download.example/result.zip",
        )

    async def download_result_zip(self, result_url: str) -> bytes:
        return self.archive


@pytest.mark.asyncio
async def test_document_service_persists_mineru_archive_and_evidence(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("full.md", "# Parsed paper\n\nBody")
        archive.writestr("content_list.json", "[]")
        archive.writestr("images/figure.png", b"PNG")

    app = create_app(data_root=tmp_path)
    async with app.router.lifespan_context(app):
        fake = _FakeMinerUClient(buffer.getvalue())
        app.state.ingestion_service.mineru_client = fake
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            upload = await client.post(
                "/api/v1/acquisitions/file",
                files={"file": ("paper.pdf", b"%PDF-1.7 fixture", "application/pdf")},
            )
            task = (await client.get(f"/api/v1/acquisitions/{upload.json()['task_id']}")).json()
            response = await client.post(
                "/api/v1/ingestions",
                json={"asset_id": task["latest_asset_id"], "external_processing_allowed": True},
            )
            assert response.status_code == 202

            runs = (await client.get("/api/v1/ingestions")).json()
            evidence = (await client.get("/api/v1/evidence")).json()[0]
            assert runs[0]["status"] == "success"
            assert "Parsed paper" in (await client.get(
                f"/api/v1/evidence/{evidence['evidence_id']}/content"
            )).text
            assert fake.uploaded == b"%PDF-1.7 fixture"
            assert Path(evidence["view_uri"], "diagnostics", "mineru-result.zip").exists()
