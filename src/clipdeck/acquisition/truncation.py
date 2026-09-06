"""Login/paywall truncation detection for acquired web pages.

Some sites (Zhihu being the verified example) answer anonymous requests with a
*legitimate but truncated* page: the fetch succeeds, the HTML renders, yet the
server deliberately omits the remainder of the article and exposes a
"阅读全文" expander that only works for a logged-in session.  The existing
content-quality validator cannot see this because the page is not empty.

Detectors are configuration-driven, mirroring ``site_profiles.json``:

    truncation_markers:  domain -> list of substrings that only appear in a
                         truncated anonymous rendering of that site's pages.

Generic markers are conservative multi-character phrases that unambiguously
signal a login gate; they apply to every domain.  A match returns a small
provenance dict so the RawAsset records *why* an upgrade was attempted.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# Phrases that only make sense as a login/paywall gate.  Keep this list
# deliberately narrow: a false positive costs one wasted logged-in refetch,
# a false negative simply leaves the archive at the anonymous version.
GENERIC_TRUNCATION_MARKERS: tuple[str, ...] = (
    "登录后查看完整",
    "登录后可见",
    "登录后可阅读全文",
    "需要登录后可",
    "此内容需要登录",
    "开通会员后查看",
    "继续阅读需登录",
)


class TruncationDetector:
    """Match fetched HTML against per-domain and generic truncation markers."""

    def __init__(
        self,
        markers_by_domain: dict[str, list[str]] | None = None,
        *,
        generic_markers: tuple[str, ...] = GENERIC_TRUNCATION_MARKERS,
    ) -> None:
        self.markers_by_domain: dict[str, list[str]] = {
            str(domain).lower(): [str(marker) for marker in markers]
            for domain, markers in (markers_by_domain or {}).items()
            if isinstance(markers, list) and markers
        }
        self.generic_markers = tuple(generic_markers)

    @staticmethod
    def _candidate_domains(url: str) -> list[str]:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            return []
        if not host:
            return []
        parts = host.split(".")
        candidates: list[str] = []
        for i in range(len(parts) - 1):
            candidates.append(".".join(parts[i:]))
        return candidates

    def detect(self, url: str, html: str) -> dict[str, str] | None:
        """Return provenance info for the first matched marker, else ``None``."""
        if not html:
            return None
        domains = self._candidate_domains(url)
        for domain in domains:
            for marker in self.markers_by_domain.get(domain, ()):
                if marker in html:
                    return {"marker": marker, "scope": "domain", "domain": domain}
        primary_domain = domains[0] if domains else ""
        for marker in self.generic_markers:
            if marker in html:
                return {"marker": marker, "scope": "generic", "domain": primary_domain}
        return None
