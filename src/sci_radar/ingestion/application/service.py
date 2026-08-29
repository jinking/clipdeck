from __future__ import annotations

import os
import re
import asyncio
import tempfile
import shutil
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from bs4 import BeautifulSoup

from sci_radar.acquisition.domain import BlobRole, RawAsset, ResourceType, utcnow
from sci_radar.acquisition.repository import SQLiteRepository
from sci_radar.acquisition.storage import LocalBlobStore, extension_for_blob
from sci_radar.ingestion.application.evidence_assembler import _simple_yaml
from sci_radar.ingestion.application.evidence_assembler import EvidenceAssembler
from sci_radar.ingestion.domain.models import (
    DerivedArtifact,
    EvidenceDocument,
    IngestionOptions,
    IngestionStatus,
)
from sci_radar.ingestion.metadata import extract_article_metadata, extract_identifiers, extract_title
from sci_radar.ingestion.pipeline_fingerprint import pipeline_fingerprint as build_fingerprint
from sci_radar.ingestion.policy import ExternalProcessingDenied, ensure_external_processing_allowed
from sci_radar.ingestion.providers.mineru.client import MinerUClient
from sci_radar.ingestion.providers.mineru.schemas import map_remote_state
from sci_radar.ingestion.repository import SQLiteIngestionRepository


@dataclass(frozen=True)
class _LocalConversion:
    markdown: str
    provider: str
    converter: str
    model: str | None
    requested_provider: str | None
    fallback_status: str


class IngestionService:
    def __init__(
        self,
        *,
        raw_repository: SQLiteRepository,
        ingestion_repository: SQLiteIngestionRepository,
        blob_store: LocalBlobStore,
        evidence_root: str | Path,
        mineru_client: MinerUClient | None = None,
        llm_extractor: Any | None = None,
        image_ocr_extractor: Any | None = None,
        poll_initial_seconds: float = 2,
        poll_max_seconds: float = 30,
        task_timeout_seconds: float = 1800,
    ):
        self.raw_repository = raw_repository
        self.ingestion_repository = ingestion_repository
        self.blob_store = blob_store
        self.evidence_root = Path(evidence_root)
        self.mineru_client = mineru_client
        self.llm_extractor = llm_extractor
        self.image_ocr_extractor = image_ocr_extractor
        self.poll_initial_seconds = poll_initial_seconds
        self.poll_max_seconds = poll_max_seconds
        self.task_timeout_seconds = task_timeout_seconds

    async def ingest(
        self,
        asset_id: UUID,
        *,
        pipeline_fingerprint: str | None = None,
        options: IngestionOptions | None = None,
    ) -> EvidenceDocument:
        options = options or IngestionOptions()
        asset = await self.raw_repository.get_asset(asset_id)
        if asset is None:
            raise KeyError(f"RawAsset {asset_id} not found")
        variant = self.pipeline_variant_for(asset, options)
        pipeline_fingerprint = pipeline_fingerprint or self.pipeline_fingerprint_for(asset, options)
        existing = await self.ingestion_repository.get_evidence_for_asset(asset_id, pipeline_fingerprint)
        if existing:
            run = await self.ingestion_repository.get_run_for_asset(asset_id, pipeline_fingerprint)
            if run and run.status not in {IngestionStatus.SUCCESS, IngestionStatus.PARTIAL}:
                await self.ingestion_repository.complete_run_with_evidence(run, existing)
            return existing
        if asset.resource_type in {ResourceType.PDF, ResourceType.WORD}:
            return await self._ingest_document(asset, pipeline_fingerprint, options)
        if asset.resource_type not in {ResourceType.TEXT, ResourceType.WEB_PAGE, ResourceType.WECHAT_ARTICLE}:
            raise ValueError(f"Ingestion converter does not support {asset.resource_type}")

        run = await self.ingestion_repository.create_run(
            asset_id=asset.asset_id,
            resource_type=asset.resource_type,
            pipeline_fingerprint=pipeline_fingerprint,
            converter_name=variant["converter"] or f"{asset.resource_type.value}_local",
            execution_options=options.model_dump(),
            status=IngestionStatus.RUNNING,
        )
        run.started_at = run.started_at or utcnow()
        run.attempt_count = max(run.attempt_count, 1)
        run.status = IngestionStatus.RUNNING
        await self.ingestion_repository.save_run(run, claimed_by="inline-single-worker")
        try:
            conversion = await self._convert_local(asset, options)
            evidence = await self._write_evidence(
                asset, run_id=str(run.run_id), fingerprint=pipeline_fingerprint,
                markdown=conversion.markdown, conversion=conversion,
            )
            await self.ingestion_repository.complete_run_with_evidence(run, evidence)
            return evidence
        except Exception as exc:
            run.status = IngestionStatus.FAILED
            run.last_error_code = type(exc).__name__
            run.last_error_message = _safe_error_message(exc)
            run.finished_at = utcnow()
            await self.ingestion_repository.save_run(run)
            raise

    async def _ingest_document(
        self,
        asset: RawAsset,
        fingerprint: str,
        options: IngestionOptions,
    ) -> EvidenceDocument:
        ensure_external_processing_allowed(
            options.external_processing_allowed,
            options.sensitive_source,
        )
        if self.mineru_client is None:
            raise RuntimeError("MinerU is not configured; set MINERU_API_TOKEN")
        if options.model_version not in {"vlm", "pipeline"}:
            raise ValueError("model_version must be vlm or pipeline")
        existing = await self.ingestion_repository.get_evidence_for_asset(asset.asset_id, fingerprint)
        if existing:
            return existing
        run = await self.ingestion_repository.create_run(
            asset_id=asset.asset_id,
            resource_type=asset.resource_type,
            pipeline_fingerprint=fingerprint,
            converter_name="mineru_document",
            execution_options=options.model_dump(),
            status=IngestionStatus.RUNNING,
        )
        run.started_at = run.started_at or utcnow()
        run.attempt_count = max(run.attempt_count, 1)
        run.status = IngestionStatus.RUNNING
        await self.ingestion_repository.save_run(run, claimed_by="inline-single-worker")
        try:
            job = await self.ingestion_repository.get_external_job(str(run.run_id))
            if job and job.result_archive_blob_id:
                result_zip = self.blob_store.path_for(job.result_archive_blob_id)
            else:
                result_zip = await self._obtain_mineru_archive(asset, run, options, job)
            evidence_id = f"ev-{asset.asset_id}-{fingerprint[:12]}"
            assembler = EvidenceAssembler(output_root=self.evidence_root / "staging")
            warnings: list[str] = []
            try:
                evidence = await assembler.assemble(
                    evidence_id=evidence_id,
                    asset_id=str(asset.asset_id),
                    raw_sha256=asset.raw_sha256,
                    source_name=asset.requested_url or asset.resource_key,
                    result_zip=result_zip,
                    pipeline_fingerprint=fingerprint,
                )
            except ValueError as exc:
                if "empty" in str(exc).lower() and asset.resource_type is ResourceType.PDF and not options.is_ocr:
                    ocr_options = options.model_copy(update={"is_ocr": True})
                    warnings.append("OCR_FALLBACK_TRIGGERED")
                    result_zip = await self._obtain_mineru_archive(asset, run, ocr_options, job=None)
                    evidence = await assembler.assemble(
                        evidence_id=evidence_id,
                        asset_id=str(asset.asset_id),
                        raw_sha256=asset.raw_sha256,
                        source_name=asset.requested_url or asset.resource_key,
                        result_zip=result_zip,
                        pipeline_fingerprint=fingerprint,
                        warnings=warnings,
                    )
                else:
                    raise

            evidence.run_id = str(run.run_id)
            evidence.asset_id = asset.asset_id
            await self._canonicalize_assembled_evidence(
                evidence, raw_parent_blob_id=asset.primary_blob.blob_id,
            )
            final_view = (
                self.evidence_root / f"{asset.fetched_at:%Y}" / f"{asset.fetched_at:%m}"
                / f"{asset.fetched_at:%d}" / evidence_id
            )
            final_view.parent.mkdir(parents=True, exist_ok=True)
            _publish_directory(Path(evidence.package_path), final_view)
            evidence.package_path = final_view
            evidence.view_uri = str(final_view.resolve())
            await self.ingestion_repository.complete_run_with_evidence(run, evidence)
            return evidence
        except Exception as exc:
            run.status = IngestionStatus.FAILED
            run.last_error_code = type(exc).__name__
            run.last_error_message = _safe_error_message(exc)
            run.finished_at = utcnow()
            await self.ingestion_repository.save_run(run)
            raise

    async def _obtain_mineru_archive(self, asset, run, options, job) -> bytes | Path:
        assert self.mineru_client is not None
        source = await self.blob_store.get(asset.primary_blob.blob_id)
        if not source:
            raise ValueError("SOURCE_FILE_EMPTY")
        if len(source) > 200 * 1024 * 1024:
            raise ValueError("SOURCE_FILE_TOO_LARGE")
        extension = ".pdf" if asset.resource_type is ResourceType.PDF else extension_for_blob(asset.primary_blob, asset.resource_type)
        data_id = f"{asset.asset_id}-{run.pipeline_fingerprint[:16]}"
        if job is None or job.remote_state == "waiting-file":
            run.status = IngestionStatus.UPLOADING
            await self.ingestion_repository.save_run(run, claimed_by="inline-single-worker")
            batch = await self.mineru_client.create_file_batch(
                files=[{"name": f"asset-{asset.asset_id}{extension}", "data_id": data_id, "is_ocr": options.is_ocr}],
                model_version=options.model_version,
                enable_formula=options.enable_formula,
                enable_table=options.enable_table,
                language=options.language,
            )
            if len(batch.file_urls) != 1:
                raise ValueError("MINERU_UPLOAD_URL_FAILED")
            job = await self.ingestion_repository.upsert_external_job(
                run_id=str(run.run_id), provider="mineru", api_version="v4",
                batch_id=batch.batch_id, data_id=data_id, remote_state="waiting-file",
                request_options=options.model_dump(exclude={"external_processing_allowed", "sensitive_source"}),
            )
            await self.mineru_client.upload_file(batch.file_urls[0], source)

        started = monotonic()
        delay = self.poll_initial_seconds
        while monotonic() - started < self.task_timeout_seconds:
            result = await self.mineru_client.get_batch_result(job.batch_id)
            local_state = map_remote_state(result.state)
            run.status = IngestionStatus.DOWNLOADING if result.state == "done" else IngestionStatus(local_state.value)
            job = await self.ingestion_repository.upsert_external_job(
                run_id=str(run.run_id), provider="mineru", api_version="v4",
                batch_id=job.batch_id, data_id=job.data_id, remote_state=result.state,
                request_options=job.request_options, poll_count=job.poll_count + 1,
                provider_error=_safe_error_message(RuntimeError(result.error_message)) if result.error_message else None,
            )
            await self.ingestion_repository.save_run(run, claimed_by="inline-single-worker")
            if result.state == "failed":
                raise RuntimeError(result.error_message or "MINERU_TASK_FAILED")
            if result.state == "done":
                if not result.full_zip_url:
                    raise ValueError("MINERU_RESULT_DOWNLOAD_FAILED")
                download_to_path = getattr(self.mineru_client, "download_result_zip_to_path", None)
                if callable(download_to_path):
                    fd, temporary = tempfile.mkstemp(prefix="mineru-result-", suffix=".zip")
                    os.close(fd)
                    temporary_path = Path(temporary)
                    try:
                        await download_to_path(result.full_zip_url, temporary_path)
                        archive_ref = await self.blob_store.put_path(
                            temporary_path, mime_type="application/zip", role=BlobRole.MINERU_RESULT_ARCHIVE,
                        )
                    finally:
                        temporary_path.unlink(missing_ok=True)
                    result_zip = Path(archive_ref.storage_uri)
                else:
                    result_zip = await self.mineru_client.download_result_zip(result.full_zip_url)
                    archive_ref = await self.blob_store.put(
                        result_zip, mime_type="application/zip", role=BlobRole.MINERU_RESULT_ARCHIVE,
                    )
                await self.ingestion_repository.upsert_external_job(
                    run_id=str(run.run_id), provider="mineru", api_version="v4",
                    batch_id=job.batch_id, data_id=job.data_id, remote_state="done",
                    request_options=job.request_options, poll_count=job.poll_count,
                    result_archive_blob_id=archive_ref.blob_id,
                )
                return result_zip
            await asyncio.sleep(delay)
            delay = min(max(delay * 2, 0.01), self.poll_max_seconds)
        raise TimeoutError("MINERU_POLL_TIMEOUT")

    async def _canonicalize_assembled_evidence(
        self, evidence: EvidenceDocument, *, raw_parent_blob_id: str,
    ) -> None:
        package = Path(evidence.package_path)
        staging = package.with_name(f".{package.name}.canonical-{uuid4().hex}")
        await asyncio.to_thread(shutil.copytree, package, staging)
        role_by_path = {
            "content.md": BlobRole.EVIDENCE_MARKDOWN,
            "meta.yaml": BlobRole.EVIDENCE_YAML,
            "diagnostics/mineru-result.zip": BlobRole.MINERU_RESULT_ARCHIVE,
        }
        new_artifacts: list[DerivedArtifact] = []
        markdown_blob_id = evidence.markdown_blob_id
        markdown_blob_uri = evidence.markdown_blob_uri
        yaml_blob_id = evidence.yaml_blob_id
        source_archive_blob_id = evidence.source_archive_blob_id
        try:
            for path in sorted(item for item in staging.rglob("*") if item.is_file()):
                logical = path.relative_to(staging).as_posix()
                role = role_by_path.get(logical, BlobRole.DERIVED_IMAGE if logical.startswith("assets/") else BlobRole.DEBUG_RESPONSE)
                ref = await self.blob_store.put_path(path, mime_type=_mime_for_path(path), role=role)
                path.chmod(0o644)
                path.unlink()
                _hardlink(ref.storage_uri, path)
                new_artifacts.append(DerivedArtifact(
                    role=role.value.upper(), blob_id=ref.blob_id,
                    parent_blob_id=source_archive_blob_id,
                    sha256=ref.sha256, size_bytes=ref.size_bytes, mime_type=ref.mime_type,
                    logical_path=logical, producer="mineru",
                ))
                if logical == "content.md":
                    markdown_blob_id = ref.blob_id
                    markdown_blob_uri = ref.storage_uri
                elif logical == "meta.yaml":
                    yaml_blob_id = ref.blob_id
                elif logical == "diagnostics/mineru-result.zip":
                    source_archive_blob_id = ref.blob_id
            _publish_directory(staging, package)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        for artifact in new_artifacts:
            if artifact.logical_path == "diagnostics/mineru-result.zip":
                artifact.parent_blob_id = raw_parent_blob_id
            else:
                artifact.parent_blob_id = source_archive_blob_id
        evidence.markdown_blob_id = markdown_blob_id
        evidence.markdown_blob_uri = markdown_blob_uri
        evidence.yaml_blob_id = yaml_blob_id
        evidence.source_archive_blob_id = source_archive_blob_id
        evidence.artifacts = new_artifacts
        evidence.view_uri = str(package.resolve())

    def pipeline_variant_for(self, asset: RawAsset, options: IngestionOptions) -> dict[str, str | None]:
        if (
            asset.resource_type in {ResourceType.WEB_PAGE, ResourceType.WECHAT_ARTICLE}
            and self.llm_extractor is not None
            and _llm_egress_allowed(options)
        ):
            return {
                "provider": _llm_provider_name(self.llm_extractor),
                "converter": f"{asset.resource_type.value}_llm",
                "model": getattr(self.llm_extractor, "model", None),
                "endpoint": getattr(self.llm_extractor, "base_url", None),
            }
        if asset.resource_type in {ResourceType.PDF, ResourceType.WORD}:
            return {"provider": "mineru", "converter": "mineru_document", "model": options.model_version}
        return {"provider": "local", "converter": f"{asset.resource_type.value}_local", "model": None}

    def pipeline_fingerprint_for(self, asset: RawAsset, options: IngestionOptions) -> str:
        """Return the durable identity of the converter selected for this asset."""
        return build_fingerprint(options, variant=self.pipeline_variant_for(asset, options))

    async def validate_recoverable_run(
        self, run: Any, options: IngestionOptions,
    ) -> bool:
        """Fail a durable LLM run rather than resume it with a different converter."""
        if not str(run.converter_name).endswith("_llm"):
            return True
        asset = await self.raw_repository.get_asset(run.asset_id)
        if asset is not None:
            current_variant = self.pipeline_variant_for(asset, options)
            current_fingerprint = self.pipeline_fingerprint_for(asset, options)
            if (
                current_variant.get("converter") == run.converter_name
                and current_fingerprint == run.pipeline_fingerprint
            ):
                return True
        run.status = IngestionStatus.FAILED
        run.last_error_code = "PIPELINE_VARIANT_UNAVAILABLE"
        run.last_error_message = "Persisted LLM pipeline is unavailable or its configuration changed"
        run.finished_at = utcnow()
        await self.ingestion_repository.save_run(run)
        return False

    async def _convert_local(self, asset: RawAsset, options: IngestionOptions) -> _LocalConversion:
        data = await self.blob_store.get(asset.primary_blob.blob_id)
        text = data.decode("utf-8", errors="replace")
        if asset.resource_type is ResourceType.TEXT:
            return _LocalConversion(
                markdown=text.replace("\r\n", "\n").replace("\r", "\n"),
                provider="local", converter="text_local", model=None,
                requested_provider=None, fallback_status="not_requested",
            )

        from sci_radar.acquisition.providers import validate_html_content_quality
        is_valid, err_tag, err_desc = validate_html_content_quality(text)
        if not is_valid:
            raise ValueError(f"HTML 质量校验未通过 ({err_tag}): {err_desc}")

        # 1. 优先尝试使用 MiniMax / LLM 提取纯净 Markdown 正文
        requested_provider = _llm_provider_name(self.llm_extractor) if self.llm_extractor else None
        requested_model = getattr(self.llm_extractor, "model", None) if self.llm_extractor else None
        fallback_status = "not_requested"
        if self.llm_extractor and asset.resource_type in {ResourceType.WEB_PAGE, ResourceType.WECHAT_ARTICLE}:
            try:
                ensure_external_processing_allowed(
                    options.external_processing_allowed,
                    options.sensitive_source,
                )
                success, llm_md = await self.llm_extractor.extract(text, url=asset.requested_url)
                if success and len(llm_md.strip()) >= 50:
                    return _LocalConversion(
                        markdown=llm_md,
                        provider=requested_provider or "llm",
                        converter=f"{asset.resource_type.value}_llm",
                        model=requested_model,
                        requested_provider=requested_provider,
                        fallback_status="not_needed",
                    )
                fallback_status = "llm_failed"
            except ExternalProcessingDenied:
                fallback_status = "policy_denied" if options.external_processing_allowed else "not_requested"
            except Exception:
                fallback_status = "llm_error"

        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "aside", "form", "iframe"]):
            tag.decompose()

        for sel in [".footer", ".header", ".nav", ".navbar", ".sidebar", ".share", ".copyright", ".menu", ".comment", ".comments", ".recommend", ".relate-news", "#fixMenuBar", "#footer", "#header", "#nav", "#sidebar"]:
            for el in soup.select(sel):
                el.decompose()

        if asset.resource_type is ResourceType.WECHAT_ARTICLE:
            root = soup.select_one("#js_content") or soup.select_one("#js_article") or soup
        else:
            specific_selectors = [
                "#ContentBody", "#content", "#main-content", "#article-content", "#article_content",
                "#article", "#detail", "#zoom", "#js_content",
                "article", "main", "[role='main']",
                ".article-content", ".article-body", ".post-content", ".entry-content",
                ".detail-content", ".content-main", ".zw-content", ".zwinfos", ".txtinfos",
                ".news-content", ".story-content", ".artical-content", ".main_content", ".article"
            ]
            root = None
            for sel in specific_selectors:
                found = soup.select_one(sel)
                if found and len(found.get_text(strip=True)) >= 60:
                    root = found
                    break

            if root is None:
                root = soup.body or soup

        art_meta = extract_article_metadata(
            html=text,
            url=asset.requested_url or asset.final_url,
            fallback_title=asset.provider_meta.get("display_name") or asset.provider_meta.get("title"),
        )
        title = art_meta.title
        md = _html_to_markdown(root).strip()
        if title and not md.startswith("# "):
            md = f"# {title}\n\n{md}"

        # Multi-modal OCR for posters and info-rich images
        ocr_sections: list[str] = []
        if self.image_ocr_extractor and asset.child_assets:
            candidate_images = [img for img in asset.child_assets if img.size_bytes >= 5120][:6]
            for idx, img_ref in enumerate(candidate_images, 1):
                try:
                    img_bytes = await self.blob_store.get(img_ref.blob_id)
                    if img_bytes:
                        has_text, ocr_text = await self.image_ocr_extractor.extract_text(img_bytes, img_ref.mime_type)
                        if has_text and ocr_text:
                            ext = extension_for_blob(img_ref)
                            target_name = f"assets/img-{idx:03d}{ext}"
                            ocr_sections.append(f"### 📷 [附图/海报文字识别: {target_name}]\n\n{ocr_text}")
                except Exception as exc:
                    logger.warning("Failed to extract OCR from image %s: %s", img_ref.blob_id, exc)

        if ocr_sections:
            md = md + "\n\n---\n## 📋 附图与海报文字提取\n\n" + "\n\n".join(ocr_sections)

        if len(md) < 10:
            raise ValueError("提取后的 Markdown 正文过短，无法形成有效证据")
        return _LocalConversion(
            markdown=md + "\n",
            provider="local", converter=f"{asset.resource_type.value}_local",
            model=requested_model if fallback_status != "not_requested" else None,
            requested_provider=requested_provider if fallback_status != "not_requested" else None,
            fallback_status=fallback_status,
        )

    async def _write_evidence(
        self,
        asset: RawAsset,
        *,
        run_id: str,
        fingerprint: str,
        markdown: str,
        conversion: _LocalConversion | None = None,
    ) -> EvidenceDocument:
        evidence_id = f"ev-{asset.asset_id}-{fingerprint[:12]}"
        markdown_ref = await self.blob_store.put(
            markdown.encode("utf-8"), mime_type="text/markdown; charset=utf-8", role=BlobRole.EVIDENCE_MARKDOWN,
        )
        identifiers = extract_identifiers(markdown)
        art_meta = extract_article_metadata(
            markdown=markdown,
            url=asset.requested_url or asset.final_url,
            fallback_title=asset.provider_meta.get("display_name") or asset.provider_meta.get("title"),
        )
        title = art_meta.title or asset.provider_meta.get("display_name") or asset.provider_meta.get("title")
        warnings: list[str] = []
        if len(markdown.strip()) < 200:
            warnings.append("CONTENT_TOO_SHORT")
        if any(p in markdown for p in ["{{title}}", "{{brTitle}}", "NaN-NaN-NaN", "{{name}}"]):
            warnings.append("UNRENDERED_TEMPLATE")
        conversion = conversion or _LocalConversion(
            markdown=markdown, provider="local", converter=f"{asset.resource_type.value}_local",
            model=None, requested_provider=None, fallback_status="not_requested",
        )
        metadata = {
            "schema_version": 2,
            "evidence_id": evidence_id,
            "title": title,
            "source": {
                "asset_id": str(asset.asset_id),
                "resource_type": asset.resource_type.value,
                "requested_url": asset.requested_url,
                "final_url": asset.final_url,
                "display_name": asset.provider_meta.get("display_name") or title,
                "platform": art_meta.platform,
                "author": art_meta.author,
            },
            "time": {
                "published_at": art_meta.published_at,
                "fetched_at": asset.fetched_at.isoformat(),
                "processed_at": utcnow().isoformat(),
            },
            "document": {
                "title": title,
                "format": asset.resource_type.value,
            },
            "identifiers": identifiers,
            "raw": {"sha256": asset.raw_sha256, "version_no": asset.version_no},
            "conversion": {
                "provider": conversion.provider,
                "converter": conversion.converter,
                "model": conversion.model,
                "requested_provider": conversion.requested_provider,
                "fallback_status": conversion.fallback_status,
                "pipeline_fingerprint": fingerprint,
                "run_id": run_id,
            },
            "quality": {
                "warnings": warnings,
            },
        }
        yaml_ref = await self.blob_store.put(
            _simple_yaml(metadata).encode("utf-8"),
            mime_type="application/yaml; charset=utf-8",
            role=BlobRole.EVIDENCE_YAML,
        )
        view = self.evidence_root / f"{asset.fetched_at:%Y}" / f"{asset.fetched_at:%m}" / f"{asset.fetched_at:%d}" / evidence_id
        view.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{evidence_id}.staging-", dir=view.parent))
        artifacts = [
            DerivedArtifact(
                role="EVIDENCE_MARKDOWN", blob_id=markdown_ref.blob_id, sha256=markdown_ref.sha256,
                size_bytes=markdown_ref.size_bytes, mime_type=markdown_ref.mime_type,
                logical_path="content.md", producer="local-converter",
            ),
            DerivedArtifact(
                role="EVIDENCE_YAML", blob_id=yaml_ref.blob_id, sha256=yaml_ref.sha256,
                size_bytes=yaml_ref.size_bytes, mime_type=yaml_ref.mime_type,
                logical_path="meta.yaml", producer="metadata",
            ),
        ]
        try:
            _hardlink(markdown_ref.storage_uri, staging / "content.md")
            _hardlink(yaml_ref.storage_uri, staging / "meta.yaml")
            for index, image in enumerate(asset.child_assets, 1):
                target = staging / "assets" / f"img-{index:03d}{extension_for_blob(image)}"
                _hardlink(image.storage_uri, target)
                artifacts.append(DerivedArtifact(
                    role="DERIVED_IMAGE", blob_id=image.blob_id, parent_blob_id=asset.primary_blob.blob_id,
                    sha256=image.sha256, size_bytes=image.size_bytes, mime_type=image.mime_type,
                    logical_path=f"assets/{target.name}", producer="raw-asset-child",
                ))
            _publish_directory(staging, view)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return EvidenceDocument(
            evidence_id=evidence_id,
            run_id=run_id,
            asset_id=asset.asset_id,
            raw_sha256=asset.raw_sha256,
            pipeline_fingerprint=fingerprint,
            markdown_blob_id=markdown_ref.blob_id,
            yaml_blob_id=yaml_ref.blob_id,
            package_path=view,
            view_uri=str(view.resolve()),
            markdown_blob_uri=markdown_ref.storage_uri,
            status=IngestionStatus.SUCCESS,
            warnings=warnings,
            artifacts=artifacts,
        )


def _mime_for_path(path: Path) -> str:
    return {
        ".md": "text/markdown; charset=utf-8",
        ".yaml": "application/yaml; charset=utf-8",
        ".json": "application/json",
        ".zip": "application/zip",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")


def _llm_egress_allowed(options: IngestionOptions) -> bool:
    try:
        ensure_external_processing_allowed(
            options.external_processing_allowed,
            options.sensitive_source,
        )
    except ExternalProcessingDenied:
        return False
    return True


def _llm_provider_name(extractor: Any) -> str:
    return str(
        getattr(extractor, "provider_name", None)
        or getattr(extractor, "provider", None)
        or type(extractor).__name__
    )


def _safe_error_message(exc: Exception) -> str:
    message = str(exc)
    message = re.sub(r"(?i)(authorization|token|seed)\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", message)
    message = re.sub(r"(https://[^\s?]+)\?[^\s]+", r"\1?[redacted]", message)
    return message[:1000]

def _hardlink(source_uri: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        os.link(source_uri, target)
    target.chmod(0o444)


def _publish_directory(staging: Path, destination: Path) -> None:
    backup = destination.with_name(f".{destination.name}.backup-{uuid4().hex}")
    moved_old = False
    published = False
    try:
        if destination.exists():
            os.replace(destination, backup)
            moved_old = True
        os.replace(staging, destination)
        published = True
    except Exception:
        if moved_old and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    finally:
        if published and backup.exists():
            shutil.rmtree(backup)


def _html_to_markdown(root) -> str:
    lines: list[str] = []
    for node in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote"]):
        text = " ".join(node.get_text(" ", strip=True).split())
        if not text:
            continue
        if re.fullmatch(r"h[1-6]", node.name):
            lines.append(f"{'#' * int(node.name[1])} {text}")
        elif node.name == "li":
            lines.append(f"- {text}")
        elif node.name == "blockquote":
            lines.append(f"> {text}")
        else:
            lines.append(text)
    return "\n\n".join(lines) or root.get_text("\n", strip=True)
