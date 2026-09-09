from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from clipdeck.acquisition.api import router
from clipdeck.acquisition.domain import ResourceType
from clipdeck.acquisition.providers import LoginBrowserProvider, ProviderResolver, SpiderBypassProvider
from clipdeck.acquisition.repository import SQLiteRepository
from clipdeck.acquisition.service import AcquisitionService
from clipdeck.acquisition.storage import LocalBlobStore
from clipdeck.acquisition.truncation import TruncationDetector
from clipdeck.ingestion import site_profiles
from clipdeck.ingestion.api import router as ingestion_router
from clipdeck.ingestion.application import IngestionService
from clipdeck.ingestion.providers.mineru.client import MinerUClient, MinerUSettings
from clipdeck.ingestion.queue import SingleIngestionWorker
from clipdeck.ingestion.repository import SQLiteIngestionRepository


PACKAGE_ROOT = Path(__file__).parent
logger = logging.getLogger(__name__)


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str, *, legacy: str | None = None, default: str | None = None) -> str | None:
    """Read a settings variable, falling back to its pre-rename legacy name."""
    value = os.getenv(name)
    if value is None and legacy:
        value = os.getenv(legacy)
    return default if value is None else value


def _https_api_base_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("LLM_BASE_URL must use HTTPS and must not contain credentials")
    return value.rstrip("/")


def create_app(data_root: str | Path | None = None, *, mineru_token: str | None = None) -> FastAPI:
    load_dotenv()
    root = Path(data_root or _env("CLIPDECK_DATA", legacy="SCI_ACQUISITION_DATA", default="./data")).resolve()
    repository = SQLiteRepository(root / "acquisition.db")
    ingestion_repository = SQLiteIngestionRepository(root / "ingestion.db")
    blob_store = LocalBlobStore(root)
    resolver = ProviderResolver()
    resolver.crawl4ai.base_directory = str(root / ".crawl4ai")

    login_provider: LoginBrowserProvider | None = None
    if _enabled(_env("CLIPDECK_LOGIN_BROWSER_ENABLED", legacy="SCI_LOGIN_BROWSER_ENABLED")):
        try:
            login_provider = LoginBrowserProvider(
                cdp_url=_env("CLIPDECK_CDP_URL", legacy="SCI_CDP_URL", default="http://127.0.0.1:9222"),
            )
        except ValueError as exc:
            logger.error("Login browser upgrade disabled: %s", exc)
    truncation_detector = TruncationDetector(site_profiles.truncation_markers())
    spider_provider: SpiderBypassProvider | None = None
    if _enabled(_env("CLIPDECK_SPIDER_BYPASS_ENABLED", legacy="SCI_SPIDER_BYPASS_ENABLED", default="true")):
        spider_provider = SpiderBypassProvider()

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
    if llm_api_key and _enabled(_env("CLIPDECK_LLM_EXTERNAL_PROCESSING_ALLOWED", legacy="SCI_LLM_EXTERNAL_PROCESSING_ALLOWED")):
        from clipdeck.ingestion.llm import LLMArticleExtractor
        llm_extractor = LLMArticleExtractor(
            api_key=llm_api_key,
            base_url=_https_api_base_url(os.getenv("LLM_BASE_URL", "https://api.minimaxi.com/v1")),
            model=os.getenv("LLM_MODEL", "MiniMax-M3"),
            thinking_mode=os.getenv("LLM_THINKING_MODE", "disabled"),
        )

    image_ocr_extractor = None
    if llm_api_key:
        from clipdeck.ingestion.llm import ImageOCRExtractor
        image_ocr_extractor = ImageOCRExtractor(
            api_key=llm_api_key,
            base_url=_https_api_base_url(os.getenv("LLM_BASE_URL", "https://api.minimaxi.com/v1")),
            model=os.getenv("LLM_OCR_MODEL", os.getenv("LLM_MODEL", "MiniMax-Text-01")),
        )

    ingestion_service = IngestionService(
        raw_repository=repository,
        ingestion_repository=ingestion_repository,
        blob_store=blob_store,
        evidence_root=root / "evidence",
        mineru_client=mineru_client,
        llm_extractor=llm_extractor,
        image_ocr_extractor=image_ocr_extractor,
        poll_initial_seconds=float(_env("CLIPDECK_MINERU_POLL_INITIAL_SECONDS", legacy="SCI_MINERU_POLL_INITIAL_SECONDS", default="2")),
        poll_max_seconds=float(_env("CLIPDECK_MINERU_POLL_MAX_SECONDS", legacy="SCI_MINERU_POLL_MAX_SECONDS", default="30")),
        task_timeout_seconds=float(_env("CLIPDECK_MINERU_TASK_TIMEOUT_SECONDS", legacy="SCI_MINERU_TASK_TIMEOUT_SECONDS", default="1800")),
    )
    ingestion_worker = SingleIngestionWorker(ingestion_service, ingestion_repository)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await repository.initialize()
        await ingestion_repository.initialize()
        max_concurrency = int(_env("CLIPDECK_MAX_CONCURRENT_ACQUISITIONS", default="5"))
        app.state.service = AcquisitionService(
            repository=repository, blob_store=blob_store, resolver=resolver,
            max_concurrency=max_concurrency,
            login_provider=login_provider,
            spider_provider=spider_provider,
            truncation_detector=truncation_detector,
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
            recover_acquisitions(), name="clipdeck-recovery",
        )
        for asset in await repository.list_assets(limit=100_000):
            if "asset_view_uri" not in asset.provider_meta:
                try:
                    await blob_store.materialize_asset_view(asset)
                    await repository.update_asset_manifest(asset)
                except OSError:
                    # The canonical blob remains valid; views are rebuildable conveniences.
                    pass
        app.state.max_upload_bytes = int(_env("CLIPDECK_MAX_UPLOAD_BYTES", legacy="SCI_MAX_UPLOAD_BYTES", default=str(1024 * 1024 * 1024)))
        yield
        if not acquisition_recovery.done():
            acquisition_recovery.cancel()
        await asyncio.gather(acquisition_recovery, return_exceptions=True)
        await ingestion_worker.close()
        await resolver.crawl4ai.close()
        if login_provider is not None:
            await login_provider.close()
        await ingestion_repository.close()
        await repository.close()

    app = FastAPI(
        title="Clipdeck",
        description="Generic raw-first web archiver: acquire sources verbatim, then compile them into reproducible Markdown evidence.",
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
    uvicorn.run("clipdeck.acquisition.main:app", host="127.0.0.1", port=8765, reload=False)
