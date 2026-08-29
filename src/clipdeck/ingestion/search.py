"""Filesystem-backed full-text search over evidence view directories.

Deliberately SQLite-free: the architecture contract states that entity bytes
never enter the database, and the evidence views on disk are already the
canonical, human-readable archive.  Search therefore scans ``content.md``
files directly, with an in-process cache keyed by (path, mtime, size) so
repeated dashboard/search requests skip unchanged documents.

Substring matching (case-folded) is used instead of tokenization, which is the
correct behaviour for CJK text where FTS word segmentation is unreliable.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 1_000_000
_TITLE_RE = re.compile(r"^title:\s*(.+?)\s*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^#\s+(.{2,150})$", re.MULTILINE)

_cache: dict[str, tuple[float, int, str, str]] = {}
_cache_lock = threading.Lock()


def evidence_title(view_dir: str | Path) -> str | None:
    """Read the display title for an evidence view (meta.yaml, else first heading)."""
    root = Path(view_dir)
    text = _read_text(root / "meta.yaml")
    if text:
        match = _TITLE_RE.search(text)
        if match:
            return match.group(1).strip().strip('"')
    content = _read_text(root / "content.md")
    if content:
        match = _HEADING_RE.search(content)
        if match:
            return match.group(1).strip()
    return None


def search(
    evidence_root: str | Path,
    query: str,
    *,
    evidence_ids: set[str] | None = None,
    limit: int = 30,
    allow_empty_query: bool = False,
) -> list[dict]:
    """Scan evidence views for documents containing every query token.

    Returns newest-first ``{evidence_id, title, snippet, path}`` records.
    With ``allow_empty_query`` and no tokens, all (tag-scoped) documents are
    returned instead, enabling browse-by-tag.
    """
    root = Path(evidence_root)
    tokens = [token.casefold() for token in query.split() if token]
    if not tokens and not allow_empty_query:
        return []
    results: list[dict] = []
    try:
        content_files = sorted(
            (path for path in root.rglob("content.md") if _viewable(path)),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    for path in content_files:
        if len(results) >= limit:
            break
        evidence_id = path.parent.name
        if evidence_ids is not None and evidence_id not in evidence_ids:
            continue
        content = _read_text(path)
        if not content:
            continue
        folded = content.casefold()
        positions = [folded.find(token) for token in tokens]
        if any(position < 0 for position in positions):
            continue
        title = evidence_title(path.parent) or _heading(content) or evidence_id
        results.append({
            "evidence_id": evidence_id,
            "title": title,
            "snippet": _snippet(content, positions[0]) if positions and positions[0] >= 0 else None,
            "path": str(path.parent),
        })
    return results


def _viewable(path: Path) -> bool:
    parts = {item.lower() for item in path.parts}
    return not any(part.startswith(".") or part == "staging" for part in parts)


def _heading(content: str) -> str | None:
    match = _HEADING_RE.search(content)
    return match.group(1).strip() if match else None


def _snippet(content: str, position: int, *, radius: int = 60) -> str:
    start = max(0, position - radius)
    end = min(len(content), position + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    fragment = " ".join(content[start:end].split())
    return f"{prefix}{fragment}{suffix}"


def _read_text(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _cache.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    try:
        data = path.read_bytes()[:_MAX_FILE_BYTES]
        text = data.decode("utf-8", errors="replace")
    except OSError:
        return None
    with _cache_lock:
        if len(_cache) > 8_000:
            _cache.clear()
        _cache[key] = (stat.st_mtime, stat.st_size, text, key)
    return text
