from __future__ import annotations

import io
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath


class UnsafeArchiveError(ValueError):
    pass


def safe_extract_zip(
    archive: bytes | Path,
    destination: Path,
    *,
    max_members: int = 10_000,
    max_uncompressed_bytes: int = 1024 * 1024 * 1024,
    max_member_bytes: int = 256 * 1024 * 1024,
) -> list[Path]:
    source = io.BytesIO(archive) if isinstance(archive, bytes) else archive
    try:
        zip_file = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError) as exc:
        raise UnsafeArchiveError("invalid ZIP archive") from exc

    extracted: list[Path] = []
    with zip_file:
        members = zip_file.infolist()
        if len(members) > max_members:
            raise UnsafeArchiveError("archive member limit exceeded")
        total = sum(member.file_size for member in members)
        if total > max_uncompressed_bytes:
            raise UnsafeArchiveError("archive uncompressed size limit exceeded")

        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        for member in members:
            name = PurePosixPath(member.filename)
            mode = member.external_attr >> 16
            entry_type = stat.S_IFMT(mode)
            if (
                name.is_absolute()
                or ".." in name.parts
                or stat.S_ISLNK(mode)
                or (entry_type and entry_type not in {stat.S_IFREG, stat.S_IFDIR})
                or member.file_size > max_member_bytes
            ):
                raise UnsafeArchiveError(f"unsafe archive entry: {member.filename}")
            target = (destination / Path(*name.parts)).resolve()
            if target != root and root not in target.parents:
                raise UnsafeArchiveError(f"archive entry escapes destination: {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zip_file.open(member) as source_handle, target.open("wb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle)
            extracted.append(target)
    return extracted
