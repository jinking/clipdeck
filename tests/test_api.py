import pytest
from httpx import ASGITransport, AsyncClient

from sci_radar.acquisition.main import create_app


@pytest.mark.asyncio
async def test_text_submission_and_dashboard_summary(tmp_path) -> None:
    app = create_app(data_root=tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/acquisitions/text", json={"text": "hello", "display_name": "note"})
            assert response.status_code == 201
            task_id = response.json()["task_id"]

            task = await client.get(f"/api/v1/acquisitions/{task_id}")
            assert task.status_code == 200
            assert task.json()["status"] == "success"

            summary = await client.get("/api/v1/dashboard/summary")
            assert summary.json()["assets"] == 1


@pytest.mark.asyncio
async def test_file_asset_can_be_inspected_and_downloaded(tmp_path) -> None:
    app = create_app(data_root=tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/acquisitions/file",
                files={"file": ("paper.pdf", b"%PDF raw", "application/pdf")},
                data={"display_name": "原始论文"},
            )
            assert response.status_code == 201
            task_id = response.json()["task_id"]
            task = (await client.get(f"/api/v1/acquisitions/{task_id}")).json()
            asset_id = task["latest_asset_id"]
            blob_id = task["staged_blob"]["blob_id"]

            assets = await client.get("/api/v1/raw-assets")
            assert assets.json()[0]["resource_type"] == "pdf"
            asset = await client.get(f"/api/v1/raw-assets/{asset_id}")
            assert asset.status_code == 200
            blob = await client.get(f"/api/v1/blobs/{blob_id}")
            assert blob.content == b"%PDF raw"
            assert blob.headers["content-type"] == "application/pdf"
            assert blob.headers["content-disposition"].startswith("attachment;")
            assert blob.headers["x-content-type-options"] == "nosniff"
            assert blob.headers["content-security-policy"].startswith("sandbox")

            refetch = await client.post(f"/api/v1/raw-assets/{asset_id}/refetch")
            assert refetch.status_code == 409
            assert (await client.get("/api/v1/health")).json()["status"] == "ok"


@pytest.mark.asyncio
async def test_url_submission_rejects_non_http_scheme(tmp_path) -> None:
    app = create_app(data_root=tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/acquisitions", json={"url": "file:///etc/passwd"})
            assert response.status_code == 422


@pytest.mark.asyncio
async def test_file_upload_stops_when_streamed_size_limit_is_exceeded(tmp_path) -> None:
    app = create_app(data_root=tmp_path)
    async with app.router.lifespan_context(app):
        app.state.max_upload_bytes = 3
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/acquisitions/file",
                files={"file": ("too-large.pdf", b"1234", "application/pdf")},
            )
            assert response.status_code == 413
            assert (await client.get("/api/v1/raw-assets")).json() == []


@pytest.mark.asyncio
async def test_health_reports_failed_worker_and_repository(tmp_path) -> None:
    app = create_app(data_root=tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            app.state.ingestion_worker._task.cancel()
            await __import__("asyncio").gather(app.state.ingestion_worker._task, return_exceptions=True)
            response = await client.get("/api/v1/health")
            assert response.status_code == 503
            assert response.json()["status"] == "degraded"
            assert response.json()["checks"]["worker"] == "failed"
