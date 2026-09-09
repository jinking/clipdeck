from __future__ import annotations

import re

from clipdeck.ingestion.site_profiles import enabled_identifier_types, platform_name


_IDENTIFIER_PATTERNS: dict[str, re.Pattern[str]] = {
    "DOI": re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b", re.IGNORECASE),
    "PMID": re.compile(r"\b(?:PMID|PubMed(?:\s*ID)?)[ \t:]*(\d{6,9})\b", re.IGNORECASE),
    "PMCID": re.compile(r"\b(PMC\d{6,8})\b", re.IGNORECASE),
    "NCT": re.compile(r"\b(NCT\d{8})\b", re.IGNORECASE),
    "ChiCTR": re.compile(r"\b(ChiCTR(?:-[A-Za-z0-9]+)?\d{6,10})\b", re.IGNORECASE),
    "ORCID": re.compile(r"\b(\d{4}-\d{4}-\d{4}-\d{3}[\dX])\b", re.IGNORECASE),
}


def extract_identifiers(text: str) -> list[dict[str, str]]:
    """Extract configured identifiers (DOI, PMID, PMCID, NCT, ChiCTR, ORCID).

    The active extractor set is controlled by ``identifier_extractors`` in the
    site profiles config; every built-in extractor is enabled by default.
    Returns a deduplicated list of structured records.
    """
    if not text:
        return []

    active = enabled_identifier_types()
    results: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for extractor_type in _IDENTIFIER_PATTERNS:
        if extractor_type not in active:
            continue
        pattern = _IDENTIFIER_PATTERNS[extractor_type]
        for match in pattern.finditer(text):
            if extractor_type == "DOI":
                value = match.group(0).rstrip(".,;)>]")
                key = (extractor_type, value.lower())
            elif extractor_type in {"PMCID", "NCT"}:
                value = match.group(1).upper()
                key = (extractor_type, value)
            elif extractor_type == "ChiCTR":
                value = match.group(1)
                key = (extractor_type, value.upper())
            else:
                value = match.group(1)
                key = (extractor_type, value)
            if key not in seen:
                seen.add(key)
                results.append({"type": extractor_type, "value": value})

    return results


from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class ArticleMetadata:
    title: str | None = None
    published_at: str | None = None
    platform: str | None = None
    author: str | None = None


def parse_iso_or_ts(val: str | int | float | None, tz_offset_hours: int = 8) -> str | None:
    """Parse various datetime representations into ISO-8601 string."""
    if not val:
        return None
    val_str = str(val).strip()
    if val_str.isdigit():
        ts = int(val_str)
        if ts > 100000000000:
            ts = ts / 1000.0
        try:
            from datetime import timedelta, timezone
            tz = timezone(timedelta(hours=tz_offset_hours))
            return datetime.fromtimestamp(ts, tz=tz).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    # 0. ISO 8601 with timezone like 2026-02-27T08:54:29.000Z
    if "T" in val_str and (val_str.endswith("Z") or "+" in val_str or "-" in val_str[10:]):
        try:
            from datetime import timedelta, timezone
            iso_clean = val_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_clean)
            tz = timezone(timedelta(hours=tz_offset_hours))
            return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    # 1. ISO 8601 string like 2024-10-18T08:30:00+08:00
    m_iso = re.search(r"(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])[T\s](\d{1,2}:\d{2}(?::\d{2})?)", val_str)
    if m_iso:
        y, mon, d, t = m_iso.groups()
        if len(t) == 5:
            t = f"{t}:00"
        return f"{y}-{int(mon):02d}-{int(d):02d} {t}"

    # 2. Chinese formatted date like 2026年07月15日 13:51
    m = re.search(r"(20\d{2})[-/年](0?[1-9]|1[0-2])[-/月](0?[1-9]|[12]\d|3[01])[日\s]*(\d{1,2}:\d{2}(?::\d{2})?)?", val_str)
    if m:
        y, mon, d, t = m.groups()
        time_part = f" {t}" if t else " 00:00:00"
        if t and len(t) == 5:
            time_part = f" {t}:00"
        return f"{y}-{int(mon):02d}-{int(d):02d}{time_part}"

    return None


def extract_title(markdown: str | None = None, *, html: str | None = None, fallback: str | None = None) -> str | None:
    """Extract article title from HTML, Markdown, or fallback metadata."""
    meta = extract_article_metadata(html=html, markdown=markdown, fallback_title=fallback)
    return meta.title or fallback


def extract_article_metadata(
    *,
    html: str | None = None,
    markdown: str | None = None,
    url: str | None = None,
    fallback_title: str | None = None,
) -> ArticleMetadata:
    """Extract comprehensive structured article metadata (title, published_at, platform, author)."""
    meta = ArticleMetadata()
    soup = None
    if html:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

    # --- 1. 标题提取 ---
    if soup:
        wc_title = soup.select_one("#activity-name, h1.rich_media_title")
        if wc_title and wc_title.get_text(strip=True):
            meta.title = wc_title.get_text(strip=True)
        elif og_title := soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"}):
            meta.title = og_title.get("content", "").strip()
        elif h1 := soup.find("h1"):
            t = h1.get_text(strip=True)
            if t and len(t) <= 150:
                meta.title = t
        elif soup.title and soup.title.get_text(strip=True):
            t = soup.title.get_text(strip=True)
            for sep in [" - ", " | ", "_微信公众平台", "—", "_"]:
                if sep in t:
                    t = t.split(sep, 1)[0].strip()
            meta.title = t

    if not meta.title and markdown:
        for line in markdown.splitlines():
            s = line.strip()
            if s.startswith("# ") and not s.startswith("##"):
                t = s.removeprefix("# ").strip()
                if t and len(t) > 2:
                    meta.title = t
                    break
        if not meta.title:
            for line in markdown.splitlines()[:5]:
                l = line.strip().lstrip("#").strip("*").strip()
                if 3 <= len(l) <= 120 and not l.startswith("http") and not l.startswith("![") and not l.startswith(">"):
                    meta.title = l
                    break

    if not meta.title and fallback_title and fallback_title not in {"微信公众号文章", "未命名", "网页", "文档"} and not fallback_title.startswith("http"):
        meta.title = fallback_title.strip()

    # --- 2. 发布时间提取 ---
    if soup:
        for meta_name in [
            "article:published_time", "pubdate", "publishdate", "date",
            "citation_publication_date", "citation_date", "og:release_date", "sailthru.date",
        ]:
            tag = soup.find("meta", property=meta_name) or soup.find("meta", attrs={"name": meta_name})
            if tag and tag.get("content"):
                parsed = parse_iso_or_ts(tag["content"])
                if parsed:
                    meta.published_at = parsed
                    break

    if not meta.published_at and html and url and "mp.weixin.qq.com" in url:
        m_ts = re.search(r"(?:var\s+createTimestamp|createTimestamp|ori_create_time|var\s+ct)\s*[:=]\s*[\'\"]?(\d{10})[\'\"]?", html)
        if m_ts:
            meta.published_at = parse_iso_or_ts(m_ts.group(1), tz_offset_hours=8)
        if not meta.published_at:
            m_str = re.search(r"(?:var\s+createTime|createTime)\s*=\s*[\'\"]([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2}(?::[0-9]{2})?)[\'\"]", html)
            if m_str:
                meta.published_at = parse_iso_or_ts(m_str.group(1))

    if not meta.published_at and url and "zhihu.com" in url and soup:
        zh_times = soup.select("[itemprop='dateCreated'], [itemprop='datePublished']")
        if zh_times:
            meta.published_at = parse_iso_or_ts(zh_times[-1].get("content"), tz_offset_hours=8)

    # 2.3 DOM 常见时间容器与通用时间标签匹配
    if not meta.published_at and soup:
        for sel in [
            "time", ".publish-time", ".time", ".date", ".info", ".post-date",
            ".article-time", "#publish_time", ".news-time", ".source-time", ".head-time", ".item",
            ".subtitle", "#source", ".article-info", ".detail-info",
        ]:
            for el in soup.select(sel)[:5]:
                parsed = parse_iso_or_ts(el.get("datetime") or el.get_text())
                if parsed:
                    meta.published_at = parsed
                    break
            if meta.published_at:
                break

    # 2.4 通用 DOM 文本内时间探测 (如 "转自：2026-03-13 16:05:35", "发布时间: 2026年3月17日")
    if not meta.published_at and soup:
        for el in soup.find_all(["div", "span", "p", "time"]):
            t = el.get_text(strip=True)
            if 6 <= len(t) <= 60 and any(k in t for k in ["转自：", "转自:", "发布于", "发布时间", "时间：", "时间:"]):
                parsed = parse_iso_or_ts(t)
                if parsed and "星期" not in t:
                    meta.published_at = parsed
                    break

    if not meta.published_at and url:
        m_url = re.search(r"/(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])", url)
        if m_url:
            y, mon, d = m_url.groups()
            meta.published_at = f"{y}-{int(mon):02d}-{int(d):02d} 00:00:00"
        else:
            m_compact = re.search(r"/(20\d{2})(0[1-9]|1[0-2])([0-3]\d)/", url)
            if m_compact:
                y, mon, d = m_compact.groups()
                meta.published_at = f"{y}-{int(mon):02d}-{int(d):02d} 00:00:00"

    # --- 3. 平台提取 ---
    domain = url.split("://")[-1].split("/")[0] if (url and "://" in url) else ""
    configured_platform = platform_name(domain)
    if configured_platform:
        meta.platform = configured_platform
    elif soup and (og_site := soup.find("meta", property="og:site_name")):
        meta.platform = og_site.get("content", "").strip()
    elif soup and soup.title:
        full_title = soup.title.get_text(strip=True)
        for sep in [" - ", " | ", "_", "——", "·"]:
            if sep in full_title:
                meta.platform = full_title.split(sep)[-1].strip()
                break
    if not meta.platform and domain:
        meta.platform = domain

    # --- 4. 作者与来源机构提取 ---
    if url and "mp.weixin.qq.com" in url:
        if soup:
            wc_nick = soup.select_one("#js_name, #profileBt, .profile_nickname, strong.account_nickname")
            if wc_nick and wc_nick.get_text(strip=True):
                meta.author = wc_nick.get_text(strip=True)
        if not meta.author and html:
            m_nick = re.search(r"var\s+nickname\s*=\s*(?:htmlDecode\()?[\'\"]([^\'\"]+)[\'\"]", html)
            if m_nick:
                meta.author = m_nick.group(1).strip()
            else:
                m_nick2 = re.search(r"nickname\s*[:=]\s*(?:htmlDecode\()?[\'\"]([^\'\"]+)[\'\"]", html)
                if m_nick2 and m_nick2.group(1).strip() not in {"未命名账号", ""}:
                    meta.author = m_nick2.group(1).strip()

    if not meta.author and url and "zhihu.com" in url and soup:
        zh_author = soup.select_one(".AuthorInfo-name, [itemprop='author'] meta[itemprop='name']")
        if zh_author:
            name = zh_author.get("content") or zh_author.get_text()
            if name and name.strip():
                meta.author = name.strip()

    if not meta.author and soup:
        for meta_name in ["author", "article:author", "citation_author", "dc.creator", "byl"]:
            tag = soup.find("meta", property=meta_name) or soup.find("meta", attrs={"name": meta_name})
            if tag and tag.get("content") and len(tag["content"]) <= 50:
                meta.author = tag["content"].strip()
                break

    if not meta.author and soup:
        for sel in [".author", ".source", ".article-author", ".info", ".origin", ".copy-from", ".author-name", ".source-name"]:
            for el in soup.select(sel)[:5]:
                txt = el.get_text(strip=True)
                m = re.search(r"(?:来源|作者|文/|记者|本文来源|来源机构)[：:\s]+([^\s·|/]{2,30})", txt)
                if m:
                    meta.author = m.group(1).strip()
                    break
            if meta.author:
                break

    if not meta.author and soup:
        for p in soup.find_all(["p", "span", "div"]):
            txt = p.get_text(strip=True)
            if 3 <= len(txt) <= 50 and any(k in txt for k in ["作者：", "作者:", "记者：", "记者:", "来源：", "来源:"]):
                m = re.search(r"(?:作者|记者|来源)[：:\s]+([^\s·|/]{2,30})", txt)
                if m:
                    meta.author = m.group(1).strip()
                    break

    return meta
