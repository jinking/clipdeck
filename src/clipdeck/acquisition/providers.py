from __future__ import annotations

import asyncio
import base64
import inspect
import socket
from email.message import Message
from http.cookiejar import CookieJar
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.request import Request as CookieRequest

import httpx
from bs4 import BeautifulSoup

from clipdeck.acquisition.domain import (
    BlobRole,
    ErrorCode,
    ProviderFetchResult,
    ProviderPayload,
    ResourceType,
    ValidationStatus,
)
from clipdeck.acquisition.encoding import decode_html
from clipdeck.acquisition.security import UnsafeTargetError, resolve_public_http_url, validate_public_http_url


class AcquisitionProvider(ABC):
    @abstractmethod
    async def fetch(self, url: str, *, capture_screenshot: bool = False) -> ProviderFetchResult:
        raise NotImplementedError


def error_for_status(status: int) -> tuple[str, bool]:
    if status == 403:
        return ErrorCode.HTTP_403, True
    if status == 404:
        return ErrorCode.HTTP_404, False
    if status == 429:
        return ErrorCode.HTTP_429, True
    if status >= 500:
        return ErrorCode.HTTP_5XX, True
    return ErrorCode.HTTP_ERROR, False


def validate_html_content_quality(
    content: bytes | str, content_type: str | None = None,
) -> tuple[bool, str | None, str | None]:
    """Validate whether captured HTML contains meaningful text or is an empty/blocked skeleton."""
    if isinstance(content, bytes):
        text_html = decode_html(content, content_type)
    else:
        text_html = str(content)

    # 1. 优先检测明确的错误与拦截特征（不限长度）
    if "DOI Not Found" in text_html:
        return False, "DOI_NOT_FOUND", "DOI 系统未找到对应论文 (DOI 404)"
    if "Documents Download Module" in text_html and "being prepared for download" in text_html:
        return False, "DOWNLOAD_INTERMEDIATE_PAGE", "仅抓取到文件下载中间凭证页，未直连到目标文件"
    if "cf-browser-verification" in text_html or ("Just a moment..." in text_html and "Cloudflare" in text_html):
        return False, "CLOUDFLARE_BLOCKED", "触发 Cloudflare 验证盾拦截"
    if "NaN-NaN-NaN" in text_html:
        return False, "SPA_UNRENDERED_SKELETON", "SPA 前端单页应用数据未加载完成 (存在 NaN-NaN-NaN 占位符)"
    if "{{title}}" in text_html or "{{brTitle}}" in text_html:
        return False, "SPA_TEMPLATE_UNRENDERED", "页面为前端未渲染模板 (存在 {{title}} 占位符)"

    soup = BeautifulSoup(text_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "aside", "form", "iframe"]):
        tag.decompose()

    for sel in [".footer", ".header", ".nav", ".navbar", ".sidebar", ".share", ".copyright", ".login", ".menu", "#footer", "#header", "#nav"]:
        for el in soup.select(sel):
            el.decompose()

    body = soup.find("body") or soup
    visible_text = body.get_text(separator=" ", strip=True)

    if len(visible_text) == 0:
        return False, "CONTENT_EMPTY", "抓取到的正文内容为空"

    # 2. 短文本或骨架屏检测
    if len(visible_text) < 300:
        if "Loading..." in text_html or "Loading …" in text_html:
            return False, "SPA_SKELETON_NOT_RENDERED", "页面为 SPA 骨架屏且未渲染正文 (Nuxt/Vue Loading 占位)"
        if "JavaScript is disabled" in text_html or "Please enable JavaScript" in text_html or "Enable JavaScript" in text_html:
            return False, "JAVASCRIPT_DISABLED_OR_BLOCKED", "页面被反爬拦截 (要求启用 JavaScript)"
        if "404 Not Found" in text_html or "Page Not Found" in text_html:
            return False, "PAGE_NOT_FOUND", "目标页面不存在 (404 Not Found)"

        # 如果含有登录/版权等噪音词，且剔除后有效字符极短
        noise_keywords = ["Copyright", "ICP备", "公网安备", "许可证号", "扫码登录", "验证码登录", "举报电话", "有害信息举报", "用户协议", "隐私政策"]
        clean_text = visible_text
        has_noise = any(kw in visible_text for kw in noise_keywords)
        if has_noise:
            for kw in noise_keywords:
                clean_text = clean_text.replace(kw, "")
            if len("".join(clean_text.split())) < 30:
                return False, "NO_MEANINGFUL_CONTENT", "页面仅包含登录/版权备案信息，缺乏有效正文"

        # 兜底：无任何已知拦截特征但可见文本过短，一律拒绝。此前该分支默认放行，
        # 导致 JS 误删正文后仅剩 <title>（约百字符）的空壳 HTML 被标记 success 静默入库。
        return False, "CONTENT_TOO_SHORT", f"可见正文过短 ({len(visible_text)} 字符)，疑似残缺或空壳捕获"

    return True, None, None


class DirectDownloadProvider(AcquisitionProvider):
    def __init__(
        self,
        *,
        max_bytes: int = 512 * 1024 * 1024,
        timeout_seconds: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
        url_validator: Callable[[str], Awaitable[None]] = validate_public_http_url,
        default_headers: dict[str, str] | None = None,
    ):
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise ValueError(
                "A custom transport can bypass pinned-origin isolation; only httpx.MockTransport is supported for tests"
            )
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.url_validator = url_validator
        self.default_headers = default_headers or {"User-Agent": "Clipdeck/0.2"}

    async def fetch(self, url: str, *, capture_screenshot: bool = False) -> ProviderFetchResult:
        del capture_screenshot
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(self.timeout_seconds),
                headers=self.default_headers,
                transport=self.transport,
                trust_env=False,
                limits=httpx.Limits(max_keepalive_connections=0),
            ) as client:
                redirect_chain: list[str] = []
                logical_cookies: dict[tuple[str, str | None, int | None], CookieJar] = {}
                current = url
                for _ in range(6):
                    request_url, request_headers, extensions = await self._validated_target(current)
                    request_headers = dict(request_headers)
                    if cookie_header := _cookie_header(logical_cookies, current):
                        request_headers["Cookie"] = cookie_header
                    async with client.stream(
                        "GET", request_url, headers=request_headers, extensions=extensions,
                    ) as response:
                        _capture_logical_cookies(logical_cookies, current, response)
                        client.cookies.clear()
                        if response.is_redirect:
                            redirect_chain.append(current)
                            location = response.headers.get("location")
                            if not location:
                                return ProviderFetchResult(
                                    success=False, requested_url=url, final_url=current,
                                    http_status=response.status_code, response_headers=dict(response.headers),
                                    redirect_chain=redirect_chain, error_code=ErrorCode.HTTP_ERROR,
                                    error_message="Redirect response is missing Location", retryable=False,
                                )
                            next_url = urljoin(current, location)
                            current = next_url
                            continue
                        if response.status_code >= 400:
                            code, retryable = error_for_status(response.status_code)
                            return ProviderFetchResult(
                                success=False, requested_url=url, final_url=current,
                                http_status=response.status_code, response_headers=dict(response.headers),
                                redirect_chain=redirect_chain, error_code=code,
                                error_message=f"HTTP status {response.status_code}", retryable=retryable,
                            )
                        if _content_length_exceeds(response, self.max_bytes):
                            return _too_large_result(url, current, response, self.max_bytes, redirect_chain)
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > self.max_bytes:
                                return _too_large_result(url, current, response, self.max_bytes, redirect_chain)
                            chunks.append(chunk)
                        data_bytes = b"".join(chunks)
                        mime = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
                        if mime.lower() in {"text/html", "application/xhtml+xml"}:
                            is_valid, err_tag, err_desc = validate_html_content_quality(data_bytes, response.headers.get("content-type"))
                            if not is_valid:
                                return ProviderFetchResult(
                                    success=False,
                                    requested_url=url,
                                    final_url=current,
                                    http_status=response.status_code,
                                    response_headers=dict(response.headers),
                                    error_code=ErrorCode.CONTENT_EMPTY_OR_BLOCKED,
                                    error_message=err_desc,
                                    retryable=False,
                                    validation_status=ValidationStatus.INVALID,
                                    provider_meta={"quality_failure": err_tag},
                                )
                        return ProviderFetchResult(
                            success=True, requested_url=url, final_url=current,
                            http_status=response.status_code, response_headers=dict(response.headers),
                            redirect_chain=redirect_chain, validation_status=ValidationStatus.VALID,
                            payloads=[ProviderPayload(data=data_bytes, role=BlobRole.SOURCE_FILE,
                                                      mime_type=mime, original_url=current, is_primary=True)],
                            provider_meta={"transport": "httpx_stream"},
                        )
                return ProviderFetchResult(success=False, requested_url=url, error_code=ErrorCode.HTTP_ERROR,
                                           error_message="Too many redirects", retryable=False)
        except UnsafeTargetError as exc:
            return ProviderFetchResult(success=False, requested_url=url, error_code=ErrorCode.UNSAFE_TARGET,
                                       error_message=str(exc), retryable=False, validation_status=ValidationStatus.BLOCKED)
        except httpx.TimeoutException as exc:
            return ProviderFetchResult(success=False, requested_url=url, error_code=ErrorCode.TIMEOUT,
                                       error_message=str(exc), retryable=True)
        except (httpx.ConnectError, socket.gaierror) as exc:
            return ProviderFetchResult(success=False, requested_url=url, error_code=ErrorCode.CONNECTION_ERROR,
                                       error_message=str(exc), retryable=True)
        except Exception as exc:
            return ProviderFetchResult(success=False, requested_url=url, error_code=ErrorCode.PROVIDER_ERROR,
                                       error_message=str(exc), retryable=False)

    async def _validated_target(self, url: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        if self.url_validator is not validate_public_http_url:
            await self.url_validator(url)
            return url, {}, {}
        addresses = await resolve_public_http_url(url)
        parts = urlsplit(url)
        assert parts.hostname is not None
        pinned = httpx.URL(url).copy_with(host=addresses[0])
        authority = parts.hostname
        if parts.port and parts.port != (443 if parts.scheme == "https" else 80):
            authority = f"{authority}:{parts.port}"
        return str(pinned), {"Host": authority}, {"sni_hostname": parts.hostname}


def _content_length_exceeds(response: httpx.Response, limit: int) -> bool:
    value = response.headers.get("content-length")
    if value is None:
        return False
    try:
        return int(value) > limit
    except ValueError:
        return False


def _logical_origin(url: str) -> tuple[str, str | None, int | None]:
    parts = urlsplit(url)
    default_port = 443 if parts.scheme == "https" else 80 if parts.scheme == "http" else None
    return parts.scheme.lower(), parts.hostname, parts.port or default_port


def _cookie_header(
    jar: dict[tuple[str, str | None, int | None], CookieJar], url: str,
) -> str | None:
    cookies = jar.get(_logical_origin(url))
    if cookies is None:
        return None
    request = CookieRequest(url)
    cookies.add_cookie_header(request)
    return request.get_header("Cookie")


def _capture_logical_cookies(
    jar: dict[tuple[str, str | None, int | None], CookieJar],
    url: str,
    response: httpx.Response,
) -> None:
    origin = _logical_origin(url)
    cookies = jar.setdefault(origin, CookieJar())
    cookies.extract_cookies(_LogicalCookieResponse(response), CookieRequest(url))


class _LogicalCookieResponse:
    def __init__(self, response: httpx.Response) -> None:
        self._headers = Message()
        for value in response.headers.get_list("set-cookie"):
            self._headers.add_header("Set-Cookie", value)

    def info(self) -> Message:
        return self._headers


def _too_large_result(
    requested_url: str, final_url: str, response: httpx.Response, limit: int,
    redirect_chain: list[str] | None = None,
) -> ProviderFetchResult:
    return ProviderFetchResult(
        success=False, requested_url=requested_url, final_url=final_url,
        http_status=response.status_code, response_headers=dict(response.headers),
        redirect_chain=redirect_chain or [], error_code=ErrorCode.RESPONSE_TOO_LARGE,
        error_message=f"Response exceeds {limit} bytes", retryable=False,
    )


class WechatArticleProvider(AcquisitionProvider):
    DELETED_MARKERS = ("内容已被发布者删除", "该内容已被发布者删除", "此内容因违规无法查看")
    BLOCKED_MARKERS = ("环境异常", "访问过于频繁", "安全验证")

    def __init__(
        self, *, child_concurrency: int = 5, max_child_bytes: int = 25 * 1024 * 1024,
        max_document_bytes: int = 50 * 1024 * 1024,
        timeout_seconds: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
        url_validator: Callable[[str], Awaitable[None]] = validate_public_http_url,
    ):
        self.child_concurrency = child_concurrency
        self.max_child_bytes = max_child_bytes
        self.max_document_bytes = max_document_bytes
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.url_validator = url_validator
        self._target_provider = DirectDownloadProvider(transport=transport, url_validator=url_validator)

    async def fetch(self, url: str, *, capture_screenshot: bool = False) -> ProviderFetchResult:
        del capture_screenshot
        if httpx.URL(url).host != "mp.weixin.qq.com":
            return ProviderFetchResult(success=False, requested_url=url, error_code=ErrorCode.WECHAT_INVALID_PAGE,
                                       error_message="Not a WeChat article URL")
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131 Safari/537.36",
                "Referer": "https://mp.weixin.qq.com/",
                "Origin": "https://mp.weixin.qq.com",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            }
            async with httpx.AsyncClient(
                follow_redirects=False, timeout=self.timeout_seconds, headers=headers,
                transport=self.transport, trust_env=False,
                limits=httpx.Limits(max_keepalive_connections=0),
            ) as client:
                redirect_chain: list[str] = []
                logical_cookies: dict[tuple[str, str | None, int | None], CookieJar] = {}
                current = url
                for _ in range(6):
                    request_url, request_headers, extensions = await self._target_provider._validated_target(current)
                    request_headers = dict(request_headers)
                    if cookie_header := _cookie_header(logical_cookies, current):
                        request_headers["Cookie"] = cookie_header
                    async with client.stream(
                        "GET", request_url, headers=request_headers, extensions=extensions,
                    ) as response:
                        _capture_logical_cookies(logical_cookies, current, response)
                        client.cookies.clear()
                        if response.is_redirect:
                            redirect_chain.append(current)
                            location = response.headers.get("location")
                            if not location:
                                return ProviderFetchResult(
                                    success=False, requested_url=url, final_url=current,
                                    http_status=response.status_code, response_headers=dict(response.headers),
                                    redirect_chain=redirect_chain, error_code=ErrorCode.HTTP_ERROR,
                                    error_message="Redirect response is missing Location", retryable=False,
                                )
                            next_url = urljoin(current, location)
                            current = next_url
                            continue
                        if response.status_code >= 400:
                            code, retryable = error_for_status(response.status_code)
                            return ProviderFetchResult(
                                success=False, requested_url=url, final_url=current,
                                http_status=response.status_code, response_headers=dict(response.headers),
                                redirect_chain=redirect_chain, error_code=code,
                                error_message=f"HTTP status {response.status_code}", retryable=retryable,
                            )
                        if _content_length_exceeds(response, self.max_document_bytes):
                            return _too_large_result(url, current, response, self.max_document_bytes, redirect_chain)
                        body = await _read_limited(response, self.max_document_bytes)
                        if body is None:
                            return _too_large_result(url, current, response, self.max_document_bytes, redirect_chain)
                        response_headers = dict(response.headers)
                        response_status = response.status_code
                    break
                else:
                    return ProviderFetchResult(
                        success=False, requested_url=url, error_code=ErrorCode.HTTP_ERROR,
                        error_message="Too many redirects", retryable=False, redirect_chain=redirect_chain,
                    )
                text = body.decode("utf-8", errors="replace")
                validation, code = self._validate(text)
                primary = ProviderPayload(data=body, role=BlobRole.RAW_HTTP_BODY,
                                          mime_type=response_headers.get("content-type", "text/html"),
                                          original_url=current, is_primary=True)
                if validation is not ValidationStatus.VALID:
                    return ProviderFetchResult(
                        success=False, requested_url=url, final_url=current, http_status=response_status,
                        response_headers=response_headers, redirect_chain=redirect_chain, validation_status=validation,
                        payloads=[primary], error_code=code, error_message="WeChat page validation failed",
                        retryable=validation is ValidationStatus.BLOCKED,
                    )
                child_urls = self._image_urls(text, current)
                children, warnings = await self._download_children(client, child_urls)
                return ProviderFetchResult(
                    success=True, requested_url=url, final_url=current, http_status=response_status,
                    response_headers=response_headers, redirect_chain=redirect_chain,
                    validation_status=validation, payloads=[primary],
                    child_payloads=children, warnings=warnings,
                    provider_meta={"child_images_discovered": len(child_urls), "child_images_saved": len(children)},
                )
        except UnsafeTargetError as exc:
            return ProviderFetchResult(success=False, requested_url=url, error_code=ErrorCode.UNSAFE_TARGET,
                                       error_message=str(exc), retryable=False, validation_status=ValidationStatus.BLOCKED)
        except Exception as exc:
            return ProviderFetchResult(success=False, requested_url=url, error_code=ErrorCode.PROVIDER_ERROR,
                                       error_message=str(exc), retryable=isinstance(exc, httpx.TransportError))

    def _validate(self, html: str) -> tuple[ValidationStatus, str | None]:
        if any(marker in html for marker in self.DELETED_MARKERS):
            return ValidationStatus.DELETED, ErrorCode.WECHAT_DELETED
        if any(marker in html for marker in self.BLOCKED_MARKERS):
            return ValidationStatus.BLOCKED, ErrorCode.WECHAT_BLOCKED
        soup = BeautifulSoup(html, "html.parser")
        if soup.select_one("#js_article") and (soup.select_one("#js_content") or "cgiDataNew" in html):
            return ValidationStatus.VALID, None
        return ValidationStatus.INVALID, ErrorCode.WECHAT_INVALID_PAGE

    def _image_urls(self, html: str, base_url: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        urls: list[str] = []
        for image in soup.select("#js_article img"):
            value = image.get("data-src") or image.get("src")
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                resolved = urljoin(base_url, value)
                if resolved not in urls:
                    urls.append(resolved)
        return urls

    async def _download_children(self, client: httpx.AsyncClient, urls: list[str]) -> tuple[list[ProviderPayload], list[str]]:
        semaphore = asyncio.Semaphore(self.child_concurrency)

        async def download(url: str) -> tuple[ProviderPayload | None, str | None]:
            try:
                async with semaphore:
                    return await self._download_child(url, headers=dict(client.headers))
            except Exception:
                return None, f"child_download_failed:{url}"

        results = await asyncio.gather(*(download(url) for url in urls))
        return [item for item, _ in results if item], [warning for _, warning in results if warning]

    async def _download_child(
        self, url: str, *, headers: dict[str, str],
    ) -> tuple[ProviderPayload | None, str | None]:
        current = url
        logical_cookies: dict[tuple[str, str | None, int | None], CookieJar] = {}
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers=headers,
            transport=self.transport,
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        ) as child_client:
            for _ in range(6):
                request_url, request_headers, extensions = await self._target_provider._validated_target(current)
                request_headers = dict(request_headers)
                if cookie_header := _cookie_header(logical_cookies, current):
                    request_headers["Cookie"] = cookie_header
                async with child_client.stream(
                    "GET", request_url, headers=request_headers, extensions=extensions,
                ) as response:
                    _capture_logical_cookies(logical_cookies, current, response)
                    child_client.cookies.clear()
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            return None, f"child_redirect_missing_location:{current}"
                        current = urljoin(current, location)
                        continue
                    if response.status_code >= 400:
                        return None, f"child_download_failed:{current}"
                    if _content_length_exceeds(response, self.max_child_bytes):
                        return None, f"child_too_large:{current}"
                    body = await _read_limited(response, self.max_child_bytes)
                    if body is None:
                        return None, f"child_too_large:{current}"
                    return ProviderPayload(
                        data=body, role=BlobRole.CHILD_IMAGE,
                        mime_type=response.headers.get("content-type"), original_url=current,
                    ), None
        return None, f"child_too_many_redirects:{url}"


async def _read_limited(response: httpx.Response, limit: int) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _dict_config_factory(**kwargs: Any) -> dict[str, Any]:
    """Small dependency-free config object used with injected crawlers."""
    return kwargs


class Crawl4AIProvider(AcquisitionProvider):
    """Crawl4AI adapter with a reusable browser and a raw HTTP fallback.

    ``adapter_name`` and ``allow_http_fallback`` are subclass hooks: the
    login-browser variant reports a distinct provenance and refuses to
    silently degrade to an anonymous fetch.

    Crawl4AI is an optional, heavyweight dependency.  The adapter therefore
    imports it lazily and keeps the dependency boundary injectable so unit
    tests do not need a Playwright browser.  A caller that owns the application
    lifecycle can call :meth:`start` and :meth:`close`; :meth:`fetch` also
    starts the crawler lazily for backwards compatibility.
    """

    adapter_name = "crawl4ai"
    allow_http_fallback = True

    def __init__(
        self,
        *,
        fallback: AcquisitionProvider | None = None,
        crawler_factory: Callable[..., Any] | None = None,
        browser_config_factory: Callable[..., Any] | None = None,
        run_config_factory: Callable[..., Any] | None = None,
        cache_mode: Any | None = None,
        capture_mhtml: bool = False,
        base_directory: str | None = None,
        url_validator: Callable[[str], Awaitable[None]] = validate_public_http_url,
    ):
        self.fallback = fallback or DirectDownloadProvider(max_bytes=50 * 1024 * 1024)
        self.crawler_factory = crawler_factory
        self.browser_config_factory = browser_config_factory
        self.run_config_factory = run_config_factory
        self.cache_mode = cache_mode
        self.capture_mhtml = capture_mhtml
        self.base_directory = base_directory
        self.url_validator = url_validator
        self._uses_default_url_validator = url_validator is validate_public_http_url

        self._crawler: Any | None = None
        self._crawler_context_entered = False
        self._started = False
        self._start_attempted = False
        self._start_error: Exception | None = None
        self._resolved_run_config_factory: Callable[..., Any] | None = None
        self._resolved_cache_mode: Any | None = None

    def _build_browser_kwargs(self) -> dict[str, Any]:
        """BrowserConfig kwargs; overridden by the login-browser variant."""
        return {
            "headless": True,
            "java_script_enabled": True,
            "accept_downloads": False,
            "ignore_https_errors": False,
            "verbose": False,
            "enable_stealth": True,
            "user_agent_mode": "random",
            "memory_saving_mode": True,
            "max_pages_before_recycle": 30,
        }

    async def _unavailable(self, url: str, *, capture_screenshot: bool, reason: str) -> ProviderFetchResult:
        """Produce the start-failure outcome: HTTP fallback or explicit failure."""
        if self.allow_http_fallback:
            return await self._fallback_fetch(url, capture_screenshot=capture_screenshot, reason=reason)
        return ProviderFetchResult(
            success=False,
            requested_url=url,
            error_code=ErrorCode.PROVIDER_ERROR,
            error_message=f"{self.adapter_name} unavailable: {reason}",
            retryable=True,
            provider_meta={"adapter": self.adapter_name},
        )

    async def start(self) -> bool:
        """Start one reusable crawler instance.

        ``False`` means that Crawl4AI or its browser runtime is unavailable;
        callers can still use :meth:`fetch`, which will use the HTTP fallback.
        """
        if self._started and self._crawler is not None:
            return True
        if self._start_attempted:
            return False

        self._start_attempted = True
        try:
            if self.crawler_factory is None:
                from crawl4ai import (  # type: ignore[import-not-found]
                    AsyncWebCrawler,
                    BrowserConfig,
                    CacheMode,
                    CrawlerRunConfig,
                )

                browser_factory = self.browser_config_factory or BrowserConfig
                crawler_factory = AsyncWebCrawler
                self._resolved_run_config_factory = self.run_config_factory or CrawlerRunConfig
                self._resolved_cache_mode = self.cache_mode if self.cache_mode is not None else CacheMode.BYPASS
            else:
                browser_factory = self.browser_config_factory
                crawler_factory = self.crawler_factory
                self._resolved_run_config_factory = self.run_config_factory or _dict_config_factory
                self._resolved_cache_mode = self.cache_mode if self.cache_mode is not None else "BYPASS"

            browser_config = None
            if browser_factory is not None:
                browser_kwargs: dict[str, Any] = self._build_browser_kwargs()
                try:
                    sig = inspect.signature(browser_factory)
                    has_var = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                    if not has_var:
                        browser_kwargs = {k: v for k, v in browser_kwargs.items() if k in sig.parameters}
                except (ValueError, TypeError):
                    pass
                browser_config = browser_factory(**browser_kwargs)

            crawler_kwargs: dict[str, Any] = {"config": browser_config}
            if self.base_directory is not None:
                crawler_kwargs["base_directory"] = self.base_directory
            crawler = crawler_factory(**crawler_kwargs)
            if inspect.isawaitable(crawler):
                crawler = await crawler

            self._crawler = crawler
            start_method = getattr(crawler, "start", None)
            if callable(start_method):
                started = start_method()
                if inspect.isawaitable(started):
                    await started
            else:
                enter_method = getattr(crawler, "__aenter__", None)
                if not callable(enter_method):
                    raise RuntimeError("Crawl4AI crawler has no start or async context manager")
                entered = enter_method()
                if inspect.isawaitable(entered):
                    entered = await entered
                if entered is not None:
                    self._crawler = entered
                self._crawler_context_entered = True

            if self._uses_default_url_validator and not self._install_request_guard():
                raise RuntimeError("Crawl4AI runtime does not expose a safe request interception hook")

            self._started = True
            return True
        except ImportError as exc:
            self._start_error = exc
            self._crawler = None
            return False
        except Exception as exc:
            self._start_error = exc
            await self._close_after_failed_start()
            return False

    def _install_request_guard(self) -> bool:
        """Block every browser navigation and subresource that is not public HTTP(S)."""
        strategy = getattr(self._crawler, "crawler_strategy", None)
        set_hook = getattr(strategy, "set_hook", None)
        if not callable(set_hook):
            return False

        async def on_page_context_created(page: Any, context: Any, **kwargs: Any) -> Any:
            del context, kwargs

            async def guard(route: Any, request: Any) -> None:
                try:
                    request_url = str(request.url)
                    await self.url_validator(request_url)
                    fetch = getattr(route, "fetch", None)
                    fulfill = getattr(route, "fulfill", None)
                    if callable(fetch) and callable(fulfill):
                        # Playwright does not route redirect hops again when a
                        # request is continued. Fetch a single hop ourselves,
                        # validate Location before exposing the response to the
                        # browser, then let the next safe hop be routed anew.
                        response = await fetch(max_redirects=0)
                        status = int(getattr(response, "status", 0))
                        headers = dict(getattr(response, "headers", {}) or {})
                        location = headers.get("location") or headers.get("Location")
                        if 300 <= status < 400 and location:
                            await self.url_validator(urljoin(request_url, location))
                        await fulfill(response=response)
                        return
                except Exception:
                    await route.abort("blockedbyclient")
                    return
                await route.continue_()

            await page.route("**/*", guard)
            return page

        set_hook("on_page_context_created", on_page_context_created)
        return True

    async def close(self) -> None:
        """Close the reusable crawler, if it was started."""
        if self._crawler is None:
            return
        try:
            if self._crawler_context_entered:
                exit_method = getattr(self._crawler, "__aexit__", None)
                if callable(exit_method):
                    exited = exit_method(None, None, None)
                    if inspect.isawaitable(exited):
                        await exited
            else:
                close_method = getattr(self._crawler, "close", None)
                if callable(close_method):
                    closed = close_method()
                    if inspect.isawaitable(closed):
                        await closed
        except Exception:
            # Browser/driver processes can already be gone during interpreter
            # or server shutdown. State cleanup must remain idempotent.
            pass
        finally:
            self._crawler = None
            self._started = False
            self._crawler_context_entered = False

    async def _close_after_failed_start(self) -> None:
        crawler = self._crawler
        self._crawler = None
        self._started = False
        self._crawler_context_entered = False
        if crawler is None:
            return
        close_method = getattr(crawler, "close", None)
        if callable(close_method):
            try:
                closed = close_method()
                if inspect.isawaitable(closed):
                    await closed
            except Exception:
                pass

    async def _fallback_fetch(self, url: str, *, capture_screenshot: bool, reason: str) -> ProviderFetchResult:
        if capture_screenshot:
            result = await self.fallback.fetch(url, capture_screenshot=capture_screenshot)
        else:
            # Keep compatibility with very small test/demonstration fallbacks
            # that only accept the URL positional argument.
            result = await self.fallback.fetch(url)
        result.warnings.append(f"{reason}_http_archive_fallback")
        result.provider_meta["adapter"] = "crawl4ai_fallback"
        result.provider_meta["fallback_reason"] = reason
        if result.payloads:
            result.payloads[0].role = BlobRole.PRIMARY_HTML
            is_valid, err_tag, err_desc = validate_html_content_quality(result.payloads[0].data)
            if not is_valid:
                result.success = False
                result.error_code = ErrorCode.CONTENT_EMPTY_OR_BLOCKED
                result.error_message = err_desc
                result.retryable = False
                result.validation_status = ValidationStatus.INVALID
                result.provider_meta["quality_failure"] = err_tag
        return result

    async def fetch(self, url: str, *, capture_screenshot: bool = False) -> ProviderFetchResult:
        # When the optional package is absent, let the HTTP fallback own its
        # normal URL validation.  This keeps the lean installation path usable
        # with fallback providers that implement their own transport checks.
        if self.crawler_factory is None and not self._start_attempted and self._uses_default_url_validator:
            if not await self.start():
                reason = "crawl4ai_not_installed" if isinstance(self._start_error, ImportError) else "crawl4ai_runtime_unavailable"
                return await self._unavailable(url, capture_screenshot=capture_screenshot, reason=reason)
        if self._start_attempted and not self._started:
            reason = "crawl4ai_not_installed" if isinstance(self._start_error, ImportError) else "crawl4ai_runtime_unavailable"
            return await self._unavailable(url, capture_screenshot=capture_screenshot, reason=reason)

        try:
            await self.url_validator(url)
        except UnsafeTargetError as exc:
            return ProviderFetchResult(
                success=False,
                requested_url=url,
                error_code=ErrorCode.UNSAFE_TARGET,
                error_message=str(exc),
                retryable=False,
                validation_status=ValidationStatus.BLOCKED,
            )
        except Exception as exc:
            return ProviderFetchResult(
                success=False,
                requested_url=url,
                error_code=ErrorCode.PROVIDER_ERROR,
                error_message=str(exc),
                retryable=False,
            )

        if not await self.start():
            reason = "crawl4ai_not_installed" if isinstance(self._start_error, ImportError) else "crawl4ai_runtime_unavailable"
            return await self._unavailable(url, capture_screenshot=capture_screenshot, reason=reason)

        try:
            if self._crawler is None or self._resolved_run_config_factory is None:
                raise RuntimeError("Crawl4AI crawler is not ready")
            config_kwargs: dict[str, Any] = {
                "cache_mode": self._resolved_cache_mode,
                "page_timeout": 60_000,
                "screenshot": capture_screenshot,
                "capture_mhtml": self.capture_mhtml,
                "magic": True,
                # 2026-08-31 根因分析：crawl4ai 的 remove_overlay_elements.js 使用子串选择器
                # [class*="overlay" i]，会命中 Squarespace 等 CMS 挂在 <body> 上的主题配置类
                # （如 isscr.org 的 tweak-portfolio-grid-overlay-*），连带 elem.remove() 删除
                # 整个 <body>，产出仅剩 <title> 的 head 空壳。遮罩清理对正文提取非必需，故关闭。
                "remove_overlay_elements": False,
                "remove_consent_popups": True,
                "delay_before_return_html": 1.5,
                "scan_full_page": True,
                "scroll_delay": 0.2,
                "max_scroll_steps": 4,
                "excluded_tags": ["nav", "footer", "header", "aside", "form"],
                "excluded_selector": ".footer, .header, .nav, .sidebar, .share, .copyright, .login, #fixMenuBar, #footer, #header",
            }
            try:
                sig = inspect.signature(self._resolved_run_config_factory)
                has_var = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                if not has_var:
                    config_kwargs = {k: v for k, v in config_kwargs.items() if k in sig.parameters}
            except (ValueError, TypeError):
                pass
            config = self._resolved_run_config_factory(**config_kwargs)
            crawl_result: Any = await self._crawler.arun(url=url, config=config)
            final_url = getattr(crawl_result, "redirected_url", None) or getattr(crawl_result, "url", None) or url
            await self.url_validator(final_url)
            response_headers = dict(getattr(crawl_result, "response_headers", None) or {})
            status_code = getattr(crawl_result, "status_code", None) or 200
            if not getattr(crawl_result, "success", False):
                return ProviderFetchResult(
                    success=False,
                    requested_url=url,
                    final_url=final_url,
                    http_status=status_code,
                    response_headers=response_headers,
                    error_code=ErrorCode.PROVIDER_ERROR,
                    error_message=getattr(crawl_result, "error_message", None) or "Crawl4AI failed",
                    retryable=True,
                    provider_meta={"adapter": self.adapter_name},
                )

            html_bytes = (getattr(crawl_result, "html", "") or "").encode("utf-8")
            is_valid, err_tag, err_desc = validate_html_content_quality(html_bytes)
            if not is_valid:
                return ProviderFetchResult(
                    success=False,
                    requested_url=url,
                    final_url=final_url,
                    http_status=status_code,
                    response_headers=response_headers,
                    error_code=ErrorCode.CONTENT_EMPTY_OR_BLOCKED,
                    error_message=err_desc,
                    retryable=False,
                    validation_status=ValidationStatus.INVALID,
                    provider_meta={"adapter": self.adapter_name, "quality_failure": err_tag},
                )

            payloads = [
                ProviderPayload(
                    data=html_bytes,
                    role=BlobRole.RENDERED_HTML,
                    mime_type="text/html; charset=utf-8",
                    original_url=final_url,
                    is_primary=True,
                )
            ]
            raw_markdown = None
            if hasattr(crawl_result, "markdown") and crawl_result.markdown:
                raw_markdown = getattr(crawl_result.markdown, "fit_markdown", None) or getattr(crawl_result.markdown, "raw_markdown", None)
            if raw_markdown and len(str(raw_markdown).strip()) > 30:
                payloads.append(
                    ProviderPayload(
                        data=str(raw_markdown).strip().encode("utf-8"),
                        role=BlobRole.FIT_MARKDOWN,
                        mime_type="text/markdown; charset=utf-8",
                        original_url=final_url,
                    )
                )
            screenshot_data = getattr(crawl_result, "screenshot", None)
            if capture_screenshot and screenshot_data:
                payloads.append(
                    ProviderPayload(
                        data=base64.b64decode(screenshot_data),
                        role=BlobRole.SCREENSHOT,
                        mime_type="image/png",
                        original_url=final_url,
                    )
                )
            mhtml_data = getattr(crawl_result, "mhtml", None)
            if self.capture_mhtml and mhtml_data:
                payloads.append(
                    ProviderPayload(
                        data=mhtml_data if isinstance(mhtml_data, bytes) else str(mhtml_data).encode("utf-8"),
                        role=BlobRole.MHTML,
                        mime_type="application/x-mimearchive",
                        original_url=final_url,
                    )
                )
            return ProviderFetchResult(
                success=True,
                requested_url=url,
                final_url=final_url,
                http_status=status_code,
                response_headers=response_headers,
                validation_status=ValidationStatus.VALID,
                payloads=payloads,
                provider_meta={"adapter": self.adapter_name},
            )
        except Exception as exc:
            return ProviderFetchResult(
                success=False,
                requested_url=url,
                error_code=ErrorCode.PROVIDER_ERROR,
                error_message=str(exc),
                retryable=True,
                provider_meta={"adapter": self.adapter_name},
            )


LOOPBACK_CDP_HOSTS = {"127.0.0.1", "localhost", "::1"}


def validate_cdp_url(cdp_url: str) -> str:
    """A CDP endpoint receives a live authenticated session; loopback only."""
    parts = urlsplit(cdp_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("CDP endpoint must be an absolute http(s) URL")
    if parts.hostname.lower() not in LOOPBACK_CDP_HOSTS:
        raise ValueError("CDP endpoint must point at loopback; remote attach is refused")
    if parts.username or parts.password:
        raise ValueError("CDP endpoint must not embed credentials")
    return cdp_url


class LoginBrowserProvider(Crawl4AIProvider):
    """Attach to a user-launched browser over CDP to fetch logged-in pages.

    Used as an *upgrade* path when the anonymous capture is detected as
    login-truncated (see :mod:`clipdeck.acquisition.truncation`).  Design
    constraints:

    * loopback-only endpoint (policy in :func:`validate_cdp_url`);
    * never kills or reconfigures the user's browser (``cdp_cleanup_on_close``
      is False and no stealth/UA overrides are applied);
    * no anonymous HTTP fallback — a failed attach must surface as a failed
      upgrade so the truncated anonymous asset stays the honest record;
    * a debug port may appear after startup (the user restarts Chrome with
      ``--remote-debugging-port``), so a failed start is retried on demand.
    """

    adapter_name = "login_browser"
    allow_http_fallback = False

    def __init__(self, *, cdp_url: str = "http://127.0.0.1:9222", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cdp_url = validate_cdp_url(cdp_url)

    def _build_browser_kwargs(self) -> dict[str, Any]:
        return {
            "cdp_url": self.cdp_url,
            "java_script_enabled": True,
            "accept_downloads": False,
            "ignore_https_errors": False,
            "verbose": False,
            "cdp_cleanup_on_close": False,
        }

    def _reset_transient_start_failure(self) -> None:
        if (
            self._start_attempted
            and not self._started
            and not isinstance(self._start_error, ImportError)
        ):
            # The user's debug browser may simply not exist yet; allow a retry.
            self._start_attempted = False
            self._start_error = None

    async def start(self) -> bool:
        self._reset_transient_start_failure()
        return await super().start()

    async def fetch(self, url: str, *, capture_screenshot: bool = False) -> ProviderFetchResult:
        # Reset before Crawl4AIProvider.fetch()'s short-circuit guard so a
        # browser that appears later (Chrome restarted with a debug port) is
        # picked up without restarting Clipdeck.
        self._reset_transient_start_failure()
        result = await super().fetch(url, capture_screenshot=capture_screenshot)
        if not result.success:
            # If the user's browser disconnected or crashed, tear down the dead crawler
            # so the next fetch will re-attach cleanly instead of hanging on stale state.
            await self.close()
        return result



DEFAULT_SPIDER_USER_AGENT = (
    "Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)"
)


class SpiderBypassProvider(DirectDownloadProvider):
    """Fetch web pages using a search-engine spider identity (e.g. Baiduspider).

    Many sites (e.g. Zhihu, Medium, paywalled news) answer search engine spiders
    with full SSR HTML to guarantee indexing, bypassing client login gates.
    """

    adapter_name = "spider_bypass"

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_SPIDER_USER_AGENT,
        url_validator: Callable[[str], Awaitable[None]] = validate_public_http_url,
        timeout_seconds: float = 15.0,
        max_bytes: int = 20 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        super().__init__(
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            transport=transport,
            url_validator=url_validator,
            default_headers=headers,
        )

    async def fetch(self, url: str, *, capture_screenshot: bool = False) -> ProviderFetchResult:
        result = await super().fetch(url, capture_screenshot=capture_screenshot)
        if result.success and result.payloads:
            for p in result.payloads:
                if p.is_primary:
                    p.role = BlobRole.RENDERED_HTML
            result.provider_meta["adapter"] = self.adapter_name
        return result


class ProviderResolver:
    def __init__(self):
        self.crawl4ai = Crawl4AIProvider()
        self.wechat = WechatArticleProvider()
        self.direct = DirectDownloadProvider()

    def resolve(self, resource_type: ResourceType) -> AcquisitionProvider:
        if resource_type is ResourceType.WECHAT_ARTICLE:
            return self.wechat
        if resource_type is ResourceType.WEB_PAGE:
            return self.crawl4ai
        return self.direct
