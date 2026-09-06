"""Optional, file-driven site/behaviour profiles.

Clipdeck keeps domain-specific tweaks out of the code.  Everything that used
to be hardcoded for a particular research project now lives in an optional
JSON config file:

    ./config/site_profiles.json           (relative to the working directory)
    $CLIPDECK_SITE_PROFILES               (explicit path, wins)

When the file is missing or invalid, the built-in defaults below apply, so the
application always starts.  Supported keys:

    platform_domains         domain -> readable platform name used in meta.yaml
    identifier_extractors    extractor name -> enabled flag (DOI, PMID, PMCID,
                             NCT, ChiCTR, ORCID)
    llm_extract_prompt_path  optional path to a custom web-article extraction
                             prompt template containing ``{content}``
    image_ocr_prompt_path    optional path to a custom image OCR prompt
                             template containing ``{content}``
    truncation_markers       domain -> substrings proving an anonymous fetch
                             was login-truncated (triggers the logged-in
                             browser upgrade in the acquisition layer)
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PLATFORM_DOMAINS: dict[str, str] = {
    "mp.weixin.qq.com": "微信公众号",
    "news.sciencenet.cn": "科学网",
    "media.qimingpian.cn": "企名片",
    "finance.eastmoney.com": "东方财富网",
    "www.yicai.com": "第一财经",
    "www.news.cn": "新华网",
    "www.xinhuanet.com": "新华网",
    "www.thepaper.cn": "澎湃新闻",
    "www.tsinghua.edu.cn": "清华大学官方网",
    "jrj.sh.gov.cn": "上海市委金融办",
    "www.eurekalert.org": "EurekAlert! 科学新闻",
    "pmc.ncbi.nlm.nih.gov": "PubMed Central (NIH)",
    "www.jfdaily.com": "上观新闻",
}

DEFAULT_IDENTIFIER_EXTRACTORS: dict[str, bool] = {
    "DOI": True,
    "PMID": True,
    "PMCID": True,
    "NCT": True,
    "ChiCTR": True,
    "ORCID": True,
}

_KNOWN_KEYS = {
    "platform_domains",
    "identifier_extractors",
    "llm_extract_prompt_path",
    "image_ocr_prompt_path",
    "truncation_markers",
}


@lru_cache(maxsize=1)
def load_profiles() -> dict:
    """Load site profiles from the optional JSON config, or defaults."""
    path = _config_path()
    profiles: dict = {
        "platform_domains": dict(DEFAULT_PLATFORM_DOMAINS),
        "identifier_extractors": dict(DEFAULT_IDENTIFIER_EXTRACTORS),
        "llm_extract_prompt_path": None,
        "image_ocr_prompt_path": None,
        "truncation_markers": {},
    }
    if path is None:
        return profiles
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("top-level JSON value must be an object")
    except Exception as exc:
        logger.warning("Ignoring invalid site profiles config %s: %s", path, exc)
        return profiles

    unknown = set(raw) - _KNOWN_KEYS
    if unknown:
        logger.warning("Unknown site profile keys ignored: %s", ", ".join(sorted(unknown)))
    domains = raw.get("platform_domains")
    if isinstance(domains, dict):
        profiles["platform_domains"].update({
            str(k).lower(): str(v) for k, v in domains.items() if isinstance(v, str)
        })
    extractors = raw.get("identifier_extractors")
    if isinstance(extractors, dict):
        profiles["identifier_extractors"].update({
            str(k).upper(): bool(v) for k, v in extractors.items()
            if str(k).upper() in DEFAULT_IDENTIFIER_EXTRACTORS
        })
    markers = raw.get("truncation_markers")
    if isinstance(markers, dict):
        profiles["truncation_markers"].update({
            str(k).lower(): [str(m) for m in v if isinstance(m, str) and m]
            for k, v in markers.items()
            if isinstance(v, list) and v
        })
    for key in ("llm_extract_prompt_path", "image_ocr_prompt_path"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            profiles[key] = value.strip()
    return profiles


def reload_profiles() -> dict:
    """Drop the cache; used by tests and after editing the config file."""
    load_profiles.cache_clear()
    return load_profiles()


def platform_name(domain: str | None) -> str | None:
    """Readable platform display name for a domain, if one is configured."""
    if not domain:
        return None
    return load_profiles()["platform_domains"].get(domain.lower())


def enabled_identifier_types() -> set[str]:
    extractors = load_profiles()["identifier_extractors"]
    return {name for name, enabled in extractors.items() if enabled}


def truncation_markers() -> dict[str, list[str]]:
    """domain -> substrings that mark a truncated anonymous rendering."""
    return load_profiles()["truncation_markers"]


def prompt_template(kind: str) -> str | None:
    """Custom LLM prompt template for ``llm_extract`` or ``image_ocr``.

    Returns the file content when configured and readable, else ``None``.
    The template must contain a ``{content}`` placeholder.
    """
    key = "llm_extract_prompt_path" if kind == "llm_extract" else "image_ocr_prompt_path"
    configured = load_profiles().get(key)
    if not configured:
        return None
    try:
        template = Path(configured).expanduser().read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Cannot read %s prompt template %s: %s", kind, configured, exc)
        return None
    if "{content}" not in template:
        logger.warning("%s prompt template %s lacks a {{content}} placeholder", kind, configured)
        return None
    return template


def _config_path() -> str | None:
    explicit = os.getenv("CLIPDECK_SITE_PROFILES")
    if explicit:
        return explicit
    candidate = Path("config/site_profiles.json")
    return str(candidate) if candidate.is_file() else None
