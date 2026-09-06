from __future__ import annotations

from typing import Annotated
import asyncio
import os
import tempfile
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from clipdeck.acquisition.domain import AcquisitionInput, BlobRole, SourceKind


class URLSubmission(BaseModel):
    url: str
    priority: int = Field(default=50, ge=0, le=100)
    force_refetch: bool = False
    capture_screenshot: bool = False
    display_name: str | None = None
    source_hint: str | None = None


class TextSubmission(BaseModel):
    text: str = Field(min_length=1, max_length=10_000_000)
    display_name: str | None = "粘贴文本"
    source_key: str | None = None


router = APIRouter(prefix="/api/v1")


def service(request: Request):
    return request.app.state.service


async def execute_task(request: Request, task_id: UUID) -> None:
    asset = await service(request).execute(task_id)
    if asset and asset.resource_type.value in {"text", "web_page", "wechat_article"}:
        try:
            await request.app.state.ingestion_worker.submit(asset.asset_id)
        except Exception:
            # Acquisition remains successful; Layer 3 persists its own failure state.
            return


class BatchSubmission(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=200)
    capture_screenshot: bool = False


@router.post("/acquisitions/batch", status_code=status.HTTP_201_CREATED)
async def submit_batch(payload: BatchSubmission, request: Request, background: BackgroundTasks):
    """Submit many URLs at once; each becomes an independent acquisition task."""
    service_instance = service(request)
    submitted: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_url in payload.urls:
        candidate = (raw_url or "").strip()
        if not candidate:
            continue
        try:
            normalized = service_instance.classifier.normalize_url(candidate)
        except ValueError as exc:
            rejected.append({"url": candidate, "reason": str(exc)})
            continue
        if normalized in seen:
            rejected.append({"url": candidate, "reason": "duplicate_in_batch"})
            continue
        seen.add(normalized)
        try:
            task = await service_instance.submit(AcquisitionInput(
                source_kind=SourceKind.URL, url=candidate,
                capture_screenshot=payload.capture_screenshot,
            ))
        except ValueError as exc:
            rejected.append({"url": candidate, "reason": str(exc)})
            continue
        submitted.append({"task_id": str(task.task_id), "url": candidate})
        background.add_task(execute_task, request, task.task_id)
    return {"submitted": submitted, "rejected": rejected}


@router.get("/save", include_in_schema=False)
async def quick_save(request: Request, background: BackgroundTasks, url: str):
    """Bookmarklet endpoint: save a URL via a plain GET and bounce back to the UI."""
    try:
        task = await service(request).submit(AcquisitionInput(source_kind=SourceKind.URL, url=url))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    background.add_task(execute_task, request, task.task_id)
    return RedirectResponse("/?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/acquisitions", status_code=status.HTTP_201_CREATED)
async def submit_url(payload: URLSubmission, request: Request, background: BackgroundTasks):
    try:
        task = await service(request).submit(AcquisitionInput(source_kind=SourceKind.URL, **payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    background.add_task(execute_task, request, task.task_id)
    return task


@router.post("/acquisitions/text", status_code=status.HTTP_201_CREATED)
async def submit_text(payload: TextSubmission, request: Request, background: BackgroundTasks):
    task = await service(request).submit(AcquisitionInput(source_kind=SourceKind.TEXT, **payload.model_dump()))
    background.add_task(execute_task, request, task.task_id)
    return task


@router.post("/acquisitions/file", status_code=status.HTTP_201_CREATED)
async def submit_file(
    request: Request,
    background: BackgroundTasks,
    file: Annotated[UploadFile, File()],
    display_name: Annotated[str | None, Form()] = None,
    source_key: Annotated[str | None, Form()] = None,
):
    fd, temporary = tempfile.mkstemp(prefix="clipdeck-upload-")
    size = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > request.app.state.max_upload_bytes:
                    raise HTTPException(status_code=413, detail="文件超过上传大小限制")
                await asyncio.to_thread(handle.write, chunk)
        if size == 0:
            raise HTTPException(status_code=422, detail="文件不能为空")
        resource_type = service(request).classifier.classify_upload(file.filename, file.content_type)
        role = BlobRole.SOURCE_FILE
        if resource_type.value == "video":
            role = BlobRole.VIDEO
        elif resource_type.value == "podcast":
            role = BlobRole.AUDIO
        staged_blob = await service(request).blob_store.put_path(
            temporary, mime_type=file.content_type, role=role,
        )
        task = await service(request).submit(AcquisitionInput(
            source_kind=SourceKind.FILE, staged_blob=staged_blob, filename=file.filename, mime_type=file.content_type,
            display_name=display_name or file.filename, source_key=source_key,
        ))
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    background.add_task(execute_task, request, task.task_id)
    return task


@router.get("/acquisitions")
async def list_tasks(request: Request, limit: int = 50, status: str | None = None):
    return await service(request).repository.list_tasks(min(max(limit, 1), 200), status=status)


@router.get("/failures")
async def get_failures(request: Request):
    return await service(request).repository.failure_analysis()


@router.get("/acquisitions/{task_id}")
async def get_task(task_id: UUID, request: Request):
    task = await service(request).repository.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {**task.model_dump(), "attempts": await service(request).repository.list_attempts(task_id)}


@router.get("/raw-assets")
async def list_assets(request: Request, limit: int = 50):
    return await service(request).repository.list_assets(min(max(limit, 1), 200))


@router.get("/raw-assets/{asset_id}")
async def get_asset(asset_id: UUID, request: Request):
    asset = await service(request).repository.get_asset(asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="RawAsset not found")
    return asset


@router.post("/raw-assets/{asset_id}/refetch", status_code=status.HTTP_201_CREATED)
async def refetch(asset_id: UUID, request: Request, background: BackgroundTasks):
    try:
        task = await service(request).refetch(asset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background.add_task(execute_task, request, task.task_id)
    return task


@router.get("/blobs/{blob_id:path}")
async def get_blob(blob_id: str, request: Request):
    from fastapi.responses import FileResponse
    from clipdeck.acquisition.storage import extension_for_blob
    normalized = blob_id if blob_id.startswith("sha256:") else f"sha256:{blob_id}"
    metadata = await service(request).repository.find_blob(normalized)
    if metadata is None or not os.path.isfile(metadata.storage_uri):
        raise HTTPException(status_code=404, detail="Blob not found")
    media_type = metadata.mime_type.split(";", 1)[0] if metadata and metadata.mime_type else "application/octet-stream"
    extension = extension_for_blob(metadata) if metadata else ".bin"
    filename = f"raw-{normalized.removeprefix('sha256:')[:12]}{extension}"
    return FileResponse(
        metadata.storage_uri,
        media_type=media_type,
        filename=filename,
        # Raw blobs are untrusted input. Always download them rather than
        # executing HTML/SVG/XML/MHTML in the application's origin.
        content_disposition_type="attachment",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


@router.get("/dashboard/summary")
async def dashboard_summary(request: Request):
    summary = await service(request).repository.summary()
    summary["providers"] = ["crawl4ai", "wechat_article", "direct_download", "local_input", "login_browser", "spider_bypass"]
    summary["future_ingestion_contracts"] = ["html_ingestion", "document_text_extraction", "media_transcription", "text_normalization"]
    return summary


@router.get("/health")
async def health(request: Request):
    checks: dict[str, str] = {}
    try:
        await request.app.state.service.repository.summary()
        await request.app.state.ingestion_repository.list_recoverable_runs()
        checks["repositories"] = "ok"
    except Exception:
        checks["repositories"] = "failed"

    worker_task = getattr(request.app.state.ingestion_worker, "_task", None)
    checks["worker"] = "ok" if worker_task is not None and not worker_task.done() else "failed"

    blob_root = request.app.state.service.blob_store.root
    try:
        def probe_blob_root() -> None:
            blob_root.mkdir(parents=True, exist_ok=True)
            fd, probe = tempfile.mkstemp(prefix=".health-", dir=blob_root)
            os.close(fd)
            os.unlink(probe)
        await asyncio.to_thread(probe_blob_root)
        checks["blob_store"] = "ok"
    except OSError:
        checks["blob_store"] = "failed"

    healthy = all(value == "ok" for value in checks.values())
    payload = {"status": "ok" if healthy else "degraded", "service": "clipdeck", "checks": checks}
    return JSONResponse(payload, status_code=200 if healthy else 503)
