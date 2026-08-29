from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

from clipdeck.acquisition.domain import utcnow
from clipdeck.ingestion.domain.models import DerivedArtifact, EvidenceDocument, IngestionStatus
from clipdeck.ingestion.metadata import extract_identifiers, extract_title
from clipdeck.ingestion.storage.safe_zip import safe_extract_zip


def _artifact(path: Path, role: str, logical_path: str) -> DerivedArtifact:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return DerivedArtifact(role=role, sha256=digest.hexdigest(), size_bytes=size,
                           logical_path=logical_path, mime_type="application/json" if path.suffix == ".json" else None,
                           producer="mineru")


class EvidenceAssembler:
    def __init__(self, *, output_root: str | Path):
        self.output_root = Path(output_root)

    async def assemble(self, *, evidence_id: str, asset_id: str, raw_sha256: str, source_name: str,
                       result_zip: bytes | Path, pipeline_fingerprint: str,
                       warnings: list[str] | None = None) -> EvidenceDocument:
        return await asyncio.to_thread(self._assemble_sync, evidence_id, asset_id, raw_sha256, source_name,
                                       result_zip, pipeline_fingerprint, warnings)

    def _assemble_sync(self, evidence_id: str, asset_id: str, raw_sha256: str, source_name: str,
                       result_zip: bytes | Path, pipeline_fingerprint: str,
                       warnings: list[str] | None = None) -> EvidenceDocument:
        self.output_root.mkdir(parents=True, exist_ok=True)
        package = self.output_root / evidence_id
        staging = self.output_root / f".{evidence_id}-{uuid4().hex}.tmp"
        extracted_root = staging / ".extracted"
        artifacts: list[DerivedArtifact] = []
        collected_warnings = list(warnings or [])
        try:
            staging.mkdir(parents=True)
            safe_extract_zip(result_zip, extracted_root)
            markdown_source = self._find_required(extracted_root, "full.md")
            markdown = markdown_source.read_text(encoding="utf-8")
            if len(markdown.strip()) < 200 and "CONTENT_TOO_SHORT" not in collected_warnings:
                collected_warnings.append("CONTENT_TOO_SHORT")
            if any(p in markdown for p in ["{{title}}", "{{brTitle}}", "NaN-NaN-NaN", "{{name}}"]) and "UNRENDERED_TEMPLATE" not in collected_warnings:
                collected_warnings.append("UNRENDERED_TEMPLATE")
            (staging / "content.md").write_text(_rewrite_image_paths(markdown), encoding="utf-8")

            for image in _mineru_images(extracted_root):
                relative = _relative_after_images(image)
                target = staging / "assets" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and target.read_bytes() != image.read_bytes():
                    raise ValueError(f"MinerU image path collision: {relative.as_posix()}")
                shutil.copyfile(image, target)
                artifacts.append(_artifact(target, "DERIVED_IMAGE", f"assets/{relative.as_posix()}"))

            diagnostics = staging / "diagnostics"
            diagnostics.mkdir(parents=True, exist_ok=True)
            archive_path = diagnostics / "mineru-result.zip"
            if isinstance(result_zip, Path):
                shutil.copyfile(result_zip, archive_path)
            else:
                archive_path.write_bytes(result_zip)
            artifacts.append(_artifact(archive_path, "MINERU_RESULT_ARCHIVE", "diagnostics/mineru-result.zip"))
            for name in ("content_list.json", "content_list_v2.json", "middle.json", "model.json"):
                matches = list(extracted_root.rglob(name))
                if matches:
                    target = diagnostics / name
                    shutil.copyfile(matches[0], target)
                    artifacts.append(_artifact(target, f"MINERU_{name.upper().replace('.', '_')}", f"diagnostics/{name}"))

            identifiers = extract_identifiers(markdown)
            title = extract_title(markdown, fallback=source_name)
            metadata = {
                "schema_version": 2,
                "evidence_id": evidence_id,
                "title": title,
                "source": {"asset_id": asset_id, "name": source_name},
                "time": {"processed_at": utcnow().isoformat()},
                "document": {"title": title, "format": Path(source_name).suffix.lstrip(".") or None},
                "identifiers": identifiers,
                "raw": {"sha256": raw_sha256},
                "conversion": {"provider": "mineru", "pipeline_fingerprint": pipeline_fingerprint},
                "artifacts": [item.logical_path for item in artifacts],
                "images": [item.logical_path for item in artifacts if item.role == "DERIVED_IMAGE"],
                "quality": {"warnings": collected_warnings},
            }
            (staging / "meta.yaml").write_text(_simple_yaml(metadata), encoding="utf-8")
            shutil.rmtree(extracted_root)
            for path in staging.rglob("*"):
                if path.is_file():
                    path.chmod(0o444)
            _publish_directory(staging, package)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        return EvidenceDocument(evidence_id=evidence_id, asset_id=asset_id, raw_sha256=raw_sha256,
                                pipeline_fingerprint=pipeline_fingerprint, package_path=package,
                                view_uri=str(package.resolve()),
                                markdown_blob_uri=str((package / "content.md").resolve()),
                                status=IngestionStatus.SUCCESS, warnings=collected_warnings, artifacts=artifacts)

    @staticmethod
    def _find_required(root: Path, name: str) -> Path:
        matches = list(root.rglob(name))
        if not matches:
            raise ValueError(f"MinerU result archive is missing {name}")
        return matches[0]


def _mineru_images(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file() and "images" in path.relative_to(root).parts)


def _relative_after_images(path: Path) -> Path:
    parts = path.parts
    index = max(i for i, part in enumerate(parts) if part == "images")
    return Path(*parts[index + 1:])


def _rewrite_image_paths(markdown: str) -> str:
    markdown = re.sub(r"(\]\()images/", r"\1assets/", markdown)
    return re.sub(r"(<img\s+[^>]*src=[\"'])images/", r"\1assets/", markdown, flags=re.IGNORECASE)


def _simple_yaml(value: object, indent: int = 0) -> str:
    prefix = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.append(_simple_yaml(item, indent + 2).rstrip())
            else:
                lines.append(f"{prefix}{key}: {json.dumps(item, ensure_ascii=False)}")
        return "\n".join(lines) + "\n"
    if isinstance(value, list):
        if not value:
            return f"{prefix}[]\n"
        return "\n".join(f"{prefix}- {json.dumps(item, ensure_ascii=False)}" for item in value) + "\n"
    return f"{prefix}{json.dumps(value, ensure_ascii=False)}\n"


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
