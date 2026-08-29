"""Character-encoding detection for captured HTML.

The web is not UTF-8-only: many Chinese sites still serve GBK/GB2312, and some
declare their charset only inside a ``<meta>`` tag.  Every place that used to
hard-decode bytes as UTF-8 now goes through :func:`decode_html`, which tries,
in order:

1. the ``Content-Type`` header charset
2. an inline ``<meta charset=...>`` / ``http-equiv`` declaration (first 4 KB)
3. ``charset-normalizer`` statistical detection over the full payload
4. UTF-8 with replacement characters as the last resort
"""

from __future__ import annotations

import re
from functools import lru_cache

_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-:.]+)""", re.IGNORECASE
)


@lru_cache(maxsize=1)
def _normalizer():
    from charset_normalizer import from_bytes  # imported lazily for fast CLI start

    return from_bytes


def detect_encoding(data: bytes, content_type: str | None = None) -> str | None:
    """Best-effort charset name for a byte payload."""
    if not data:
        return None

    if content_type:
        match = re.search(r"charset\s*=\s*\"?([a-zA-Z0-9_\-:.]+)", content_type, re.IGNORECASE)
        if match:
            return match.group(1)

    meta_match = _META_CHARSET_RE.search(data[:8192])
    if meta_match:
        return meta_match.group(1).decode("ascii", errors="ignore")

    try:
        best = _normalizer()(data).best()
    except Exception:
        return None
    if best is not None:
        return best.encoding
    return None


def decode_html(data: bytes | str, content_type: str | None = None) -> str:
    """Decode a captured HTML payload into text without mojibake."""
    if isinstance(data, str):
        return data
    if not data:
        return ""

    encoding = detect_encoding(data, content_type)
    if encoding:
        normalized = encoding.lower().replace("-", "").replace("_", "")
        effective_encoding = "utf-8-sig" if normalized in {"utf8", "utf8sig"} else encoding
        try:
            return data.decode(effective_encoding, errors="replace")
        except (LookupError, UnicodeError):
            pass
    return data.decode("utf-8-sig", errors="replace")
