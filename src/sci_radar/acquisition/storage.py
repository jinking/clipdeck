from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import shutil
from pathlib import Path

from sci_radar.acquisition.domain import BlobRef, BlobRole, RawAsset, ResourceType


MIME_EXTENSIONS = {
    "text/html": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/yaml": ".yaml",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "application/x-mimearchive": ".mhtml",
}


def extension_for_blob(blob: BlobRef, resource_type: ResourceType | None = None) -> str:
    mime = (blob.mime_type or "").split(";", 1)[0].strip().lower()
    if mime in MIME_EXTENSIONS:
        return MIME_EXTENSIONS[mime]
    if blob.role in {BlobRole.PRIMARY_HTML, BlobRole.RAW_HTTP_BODY, BlobRole.RENDERED_HTML}:
        return ".html"
    if blob.role is BlobRole.SCREENSHOT:
        return ".png"
    if resource_type is ResourceType.PDF:
        return ".pdf"
    if resource_type is ResourceType.WORD:
        return ".docx"
    if resource_type is ResourceType.TEXT:
        return ".txt"
    return ".bin"


class LocalBlobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path_for_hash(self, digest: str) -> Path:
        return self.root / "blobs" / "sha256" / digest[:2] / digest[2:4] / f"{digest}.blob"

    def path_for(self, blob_id: str) -> Path:
        return self._path_for_hash(blob_id.removeprefix("sha256:"))

    async def put(
        self,
        data: bytes,
        *,
        mime_type: str | None,
        role: BlobRole = BlobRole.SOURCE_FILE,
        original_url: str | None = None,
    ) -> BlobRef:
        digest = hashlib.sha256(data).hexdigest()
        path = self._path_for_hash(digest)

        def write_once() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                return
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".upload-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                path.chmod(0o444)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

        await asyncio.to_thread(write_once)
        return BlobRef(
            blob_id=f"sha256:{digest}",
            role=role,
            sha256=digest,
            size_bytes=len(data),
            mime_type=mime_type,
            storage_uri=str(path.resolve()),
            original_url=original_url,
        )

    async def get(self, blob_id: str) -> bytes:
        digest = blob_id.removeprefix("sha256:")
        return await asyncio.to_thread(self._path_for_hash(digest).read_bytes)

    async def put_path(
        self,
        source: str | Path,
        *,
        mime_type: str | None,
        role: BlobRole = BlobRole.SOURCE_FILE,
        original_url: str | None = None,
    ) -> BlobRef:
        source_path = Path(source)

        def store() -> tuple[str, Path, int]:
            digest_hash = hashlib.sha256()
            size = 0
            with source_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest_hash.update(chunk)
                    size += len(chunk)
            digest = digest_hash.hexdigest()
            destination = self._path_for_hash(digest)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".upload-")
                try:
                    with os.fdopen(fd, "wb") as target, source_path.open("rb") as source_handle:
                        shutil.copyfileobj(source_handle, target, length=1024 * 1024)
                        target.flush()
                        os.fsync(target.fileno())
                    os.replace(temporary, destination)
                    destination.chmod(0o444)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            return digest, destination, size

        digest, path, size = await asyncio.to_thread(store)
        return BlobRef(blob_id=f"sha256:{digest}", role=role, sha256=digest, size_bytes=size,
                       mime_type=mime_type, storage_uri=str(path.resolve()), original_url=original_url)

    async def exists(self, blob_id: str) -> bool:
        digest = blob_id.removeprefix("sha256:")
        return await asyncio.to_thread(self._path_for_hash(digest).exists)

    async def materialize_asset_view(self, asset: RawAsset) -> Path:
        """Create a human-readable hard-link view without duplicating blob bytes."""
        fetched = asset.fetched_at
        view = self.root / "raw-assets" / f"{fetched.year:04d}" / f"{fetched.month:02d}" / f"{fetched.day:02d}" / str(asset.asset_id)
        asset.provider_meta["asset_view_uri"] = str(view.resolve())

        def link(source_uri: str, target: Path) -> None:
            source = Path(source_uri)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                return
            os.link(source, target)
            source.chmod(0o444)

        def build() -> None:
            view.mkdir(parents=True, exist_ok=True)
            primary_name = f"original{extension_for_blob(asset.primary_blob, asset.resource_type)}"
            link(asset.primary_blob.storage_uri, view / primary_name)

            attachment_index = 0
            for blob in asset.blobs:
                if blob.blob_id == asset.primary_blob.blob_id and blob.role == asset.primary_blob.role:
                    continue
                attachment_index += 1
                filename = f"{attachment_index:03d}-{blob.role}{extension_for_blob(blob)}"
                link(blob.storage_uri, view / "attachments" / filename)

            for index, blob in enumerate(asset.child_assets, start=1):
                filename = f"image-{index:03d}{extension_for_blob(blob)}"
                link(blob.storage_uri, view / "images" / filename)

            manifest = view / "manifest.json"
            manifest.write_text(
                json.dumps(asset.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            manifest.chmod(0o444)

        await asyncio.to_thread(build)
        return view
