from __future__ import annotations

import asyncio
import os
import tempfile
import zipfile
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from clipdeck.ingestion import search as search_module
from clipdeck.ingestion.domain.models import IngestionOptions, IngestionStatus
from clipdeck.ingestion.policy import ExternalProcessingDenied, ensure_external_processing_allowed

router = APIRouter(prefix="/api/v1")


class IngestionSubmission(BaseModel):
    asset_id: UUID
    external_processing_allowed: bool = False
    sensitive_source: bool = False
    model_version: str = "vlm"
    language: str = "ch"
    enable_formula: bool = True
    enable_table: bool = True
    is_ocr: bool = False

    def options(self) -> IngestionOptions:
        return IngestionOptions.model_validate(self.model_dump(exclude={"asset_id"}))


async def _process(request: Request, asset_id: UUID, fingerprint: str, options: IngestionOptions) -> None:
    try:
        await request.app.state.ingestion_worker.submit(
            asset_id, fingerprint=fingerprint, options=options,
        )
    except Exception:
        # The worker persists the stable failure code/state; no secret or document body is logged here.
        return


@router.post("/ingestions", status_code=status.HTTP_202_ACCEPTED)
async def create_ingestion(payload: IngestionSubmission, request: Request, background: BackgroundTasks):
    asset = await request.app.state.service.repository.get_asset(payload.asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="RawAsset not found")
    options = payload.options()
    if asset.resource_type.value in {"pdf", "word"}:
        try:
            ensure_external_processing_allowed(
                options.external_processing_allowed, options.sensitive_source,
            )
        except ExternalProcessingDenied as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if request.app.state.ingestion_service.mineru_client is None:
            raise HTTPException(status_code=503, detail="MinerU 未配置，请设置 MINERU_API_TOKEN")
    variant = request.app.state.ingestion_service.pipeline_variant_for(asset, options)
    fingerprint = request.app.state.ingestion_service.pipeline_fingerprint_for(asset, options)
    run = await request.app.state.ingestion_repository.create_run(
        asset_id=asset.asset_id,
        resource_type=asset.resource_type,
        pipeline_fingerprint=fingerprint,
        converter_name=variant.get("converter") or asset.resource_type.value,
        execution_options=options.model_dump(),
        status=IngestionStatus.PENDING,
    )
    background.add_task(_process, request, asset.asset_id, fingerprint, options)
    return run


@router.get("/ingestions")
async def list_ingestions(request: Request, limit: int = 100):
    return await request.app.state.ingestion_repository.list_runs(min(max(limit, 1), 200))


@router.get("/ingestions/{run_id}")
async def get_ingestion(run_id: UUID, request: Request):
    run = await request.app.state.ingestion_repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="IngestionRun not found")
    job = await request.app.state.ingestion_repository.get_external_job(str(run_id))
    return {**run.model_dump(), "external_job": job}


@router.get("/evidence")
async def list_evidence(request: Request, limit: int = 100):
    repository = request.app.state.ingestion_repository
    evidence = await repository.list_evidence(min(max(limit, 1), 200))
    tag_map = await repository.tags_for_evidences([item.evidence_id for item in evidence])

    def titles() -> dict[str, str | None]:
        return {
            item.evidence_id: search_module.evidence_title(item.view_uri or item.package_path)
            for item in evidence
        }
    titles_map = await asyncio.to_thread(titles)
    return [
        {
            **item.model_dump(mode="json"),
            "tags": tag_map.get(item.evidence_id, []),
            "title": titles_map.get(item.evidence_id),
        }
        for item in evidence
    ]


@router.get("/search")
async def search_evidence(request: Request, q: str = "", tag: str | None = None, limit: int = 30):
    repository = request.app.state.ingestion_repository
    query = q.strip()
    tag_name = (tag or "").strip().lstrip("#") or None
    if not query and not tag_name:
        raise HTTPException(status_code=422, detail="Provide a search query q or a tag filter")
    evidence_ids = None
    if tag_name:
        evidence_ids = set(await repository.tag_evidence_ids(tag_name, limit=2000))
        if not evidence_ids:
            return {"query": query, "tag": tag_name, "results": []}
    root = request.app.state.ingestion_service.evidence_root
    matches = await asyncio.to_thread(
        search_module.search, root, query,
        evidence_ids=evidence_ids, limit=min(max(limit, 1), 100), allow_empty_query=True,
    )
    tag_map = await repository.tags_for_evidences([item["evidence_id"] for item in matches])
    for item in matches:
        item["tags"] = tag_map.get(item["evidence_id"], [])
        document = await repository.get_evidence(item["evidence_id"])
        item["created_at"] = document.created_at.isoformat() if document else None
        item["asset_id"] = str(document.asset_id) if document else None
        item["status"] = document.status.value if document else "unknown"
    return {"query": query, "tag": tag_name, "results": matches}


@router.get("/tags")
async def list_tags(request: Request, limit: int = 100):
    return await request.app.state.ingestion_repository.list_tags(min(max(limit, 1), 200))


class TagUpdate(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=32)


@router.put("/evidence/{evidence_id}/tags")
async def set_tags(evidence_id: str, payload: TagUpdate, request: Request):
    evidence = await _evidence_or_404(request, evidence_id)
    repository = request.app.state.ingestion_repository
    names = await repository.set_evidence_tags(evidence.evidence_id, payload.tags)
    return {"evidence_id": evidence.evidence_id, "tags": names}


async def _evidence_or_404(request: Request, evidence_id: str):
    evidence = await request.app.state.ingestion_repository.get_evidence(evidence_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return evidence


@router.get("/evidence/{evidence_id}")
async def get_evidence(evidence_id: str, request: Request):
    return await _evidence_or_404(request, evidence_id)


@router.get("/evidence/{evidence_id}/content")
async def get_evidence_content(evidence_id: str, request: Request):
    evidence = await _evidence_or_404(request, evidence_id)
    path = Path(evidence.view_uri or evidence.package_path) / "content.md"
    return FileResponse(path, media_type="text/markdown; charset=utf-8")


@router.get("/evidence/{evidence_id}/meta")
async def get_evidence_meta(evidence_id: str, request: Request):
    evidence = await _evidence_or_404(request, evidence_id)
    path = Path(evidence.view_uri or evidence.package_path) / "meta.yaml"
    return FileResponse(path, media_type="application/yaml; charset=utf-8")


@router.get("/evidence/{evidence_id}/package")
async def get_evidence_package(evidence_id: str, request: Request):
    evidence = await _evidence_or_404(request, evidence_id)
    root = Path(evidence.view_uri or evidence.package_path)
    fd, archive_name = tempfile.mkstemp(prefix="clipdeck-evidence-", suffix=".zip")
    os.close(fd)

    def build_archive() -> None:
        with zipfile.ZipFile(archive_name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                if path.name == "mineru-result.zip":
                    continue
                archive.write(path, path.relative_to(root))

    try:
        await asyncio.to_thread(build_archive)
    except Exception:
        try:
            os.unlink(archive_name)
        except FileNotFoundError:
            pass
        raise
    return FileResponse(
        archive_name,
        media_type="application/zip",
        filename=f"{evidence_id}.zip",
        background=BackgroundTask(os.unlink, archive_name),
    )
