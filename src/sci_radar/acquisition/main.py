from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from sci_radar.acquisition.api import router
from sci_radar.acquisition.domain import ResourceType
from sci_radar.acquisition.providers import ProviderResolver
from sci_radar.acquisition.repository import SQLiteRepository
from sci_radar.acquisition.service import AcquisitionService
from sci_radar.acquisition.storage import LocalBlobStore
from sci_radar.ingestion.api import router as ingestion_router
from sci_radar.ingestion.application import IngestionService
from sci_radar.ingestion.providers.mineru.client import MinerUClient, MinerUSettings
from sci_radar.ingestion.queue import SingleIngestionWorker
from sci_radar.ingestion.repository import SQLiteIngestionRepository


PACKAGE_ROOT = Path(__file__).parent


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _https_api_base_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("LLM_BASE_URL must use HTTPS and must not contain credentials")
    return value.rstrip("/")


def create_app(data_root: str | Path | None = None, *, mineru_token: str | None = None) -> FastAPI:
    load_dotenv()
    root = Path(data_root or os.getenv("SCI_ACQUISITION_DATA", "./data")).resolve()
    repository = SQLiteRepository(root / "acquisition.db")
    ingestion_repository = SQLiteIngestionRepository(root / "ingestion.db")
    blob_store = LocalBlobStore(root)
    resolver = ProviderResolver()
    resolver.crawl4ai.base_directory = str(root / ".crawl4ai")

    token = mineru_token if mineru_token is not None else os.getenv("MINERU_API_TOKEN")
    mineru_client = None
    if token:
        result_hosts = {
            item.strip().lower()
            for item in os.getenv(
                "MINERU_RESULT_HOSTS",
                "cdn-mineru.openxlab.org.cn,mineru.net,openxlab.org.cn,aliyuncs.com,volces.com",
            ).split(",")
            if item.strip()
        }
        mineru_client = MinerUClient(settings=MinerUSettings(
            token=token,
            base_url=os.getenv("MINERU_BASE_URL", "https://mineru.net"),
            result_host_allowlist=result_hosts,
            timeout_seconds=float(os.getenv("MINERU_TIMEOUT_SECONDS", "180")),
            max_result_zip_bytes=int(os.getenv("MINERU_MAX_RESULT_ZIP_BYTES", str(500 * 1024 * 1024))),
            request_retries=int(os.getenv("MINERU_REQUEST_RETRIES", "3")),
            upload_retries=int(os.getenv("MINERU_UPLOAD_RETRIES", "2")),
        ))
    llm_api_key = os.getenv("LLM_API_KEY")
    llm_extractor = None
    if llm_api_key and _enabled(os.getenv("SCI_LLM_EXTERNAL_PROCESSING_ALLOWED")):
        from sci_radar.ingestion.llm import LLMArticleExtractor
        llm_extractor = LLMArticleExtractor(
            api_key=llm_api_key,
            base_url=_https_api_base_url(os.getenv("LLM_BASE_URL", "https://api.minimaxi.com/v1")),
            model=os.getenv("LLM_MODEL", "MiniMax-Text-01"),
        )

    ingestion_service = IngestionService(
        raw_repository=repository,
        ingestion_repository=ingestion_repository,
        blob_store=blob_store,
        evidence_root=root / "evidence",
        mineru_client=mineru_client,
        llm_extractor=llm_extractor,
        poll_initial_seconds=float(os.getenv("SCI_MINERU_POLL_INITIAL_SECONDS", "2")),
        poll_max_seconds=float(os.getenv("SCI_MINERU_POLL_MAX_SECONDS", "30")),
        task_timeout_seconds=float(os.getenv("SCI_MINERU_TASK_TIMEOUT_SECONDS", "1800")),
    )
    ingestion_worker = SingleIngestionWorker(ingestion_service, ingestion_repository)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await repository.initialize()
        await ingestion_repository.initialize()
        app.state.service = AcquisitionService(
            repository=repository, blob_store=blob_store, resolver=resolver,
        )
        app.state.ingestion_repository = ingestion_repository
        app.state.ingestion_service = ingestion_service
        app.state.ingestion_worker = ingestion_worker
        await resolver.crawl4ai.start()
        await ingestion_worker.start()

        async def recover_acquisitions() -> None:
            for task in await repository.list_recoverable_tasks():
                try:
                    asset = await app.state.service.execute(task.task_id)
                    if asset and asset.resource_type in {
                        ResourceType.TEXT,
                        ResourceType.WEB_PAGE,
                        ResourceType.WECHAT_ARTICLE,
                    }:
                        await ingestion_worker.submit(asset.asset_id)
                except Exception:
                    # AcquisitionService and IngestionService persist their own
                    # stable failure states. Continue recovering later tasks.
                    continue

        acquisition_recovery = asyncio.create_task(
            recover_acquisitions(), name="sci-acquisition-recovery",
        )
        for asset in await repository.list_assets(limit=100_000):
            if "asset_view_uri" not in asset.provider_meta:
                try:
                    await blob_store.materialize_asset_view(asset)
                    await repository.update_asset_manifest(asset)
                except OSError:
                    # The canonical blob remains valid; views are rebuildable conveniences.
                    pass
        app.state.max_upload_bytes = int(os.getenv("SCI_MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
        yield
        if not acquisition_recovery.done():
            acquisition_recovery.cancel()
        await asyncio.gather(acquisition_recovery, return_exceptions=True)
        await ingestion_worker.close()
        await resolver.crawl4ai.close()
        await ingestion_repository.close()
        await repository.close()

    app = FastAPI(
        title="SCI Radar Acquisition",
        description="Raw-first acquisition plus reproducible Layer 3 Evidence ingestion.",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.include_router(router)
    app.include_router(ingestion_router)
    static_root = PACKAGE_ROOT / "static"
    app.mount("/static", StaticFiles(directory=static_root), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static_root / "index.html")

    return app


app = create_app()


def run() -> None:
    uvicorn.run("sci_radar.acquisition.main:app", host="127.0.0.1", port=8765, reload=False)
