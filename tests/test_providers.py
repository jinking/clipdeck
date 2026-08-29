import httpx
import pytest

from clipdeck.acquisition.domain import ErrorCode, ProviderFetchResult, ResourceType, ValidationStatus
from clipdeck.acquisition.providers import (
    Crawl4AIProvider,
    DirectDownloadProvider,
    ProviderResolver,
    WechatArticleProvider,
    error_for_status,
)


async def allow_public_url(url: str) -> None:
    return None


def test_providers_reject_shared_non_mock_custom_transports() -> None:
    class SharedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):  # pragma: no cover
            raise AssertionError

    with pytest.raises(ValueError, match="MockTransport|custom transport"):
        DirectDownloadProvider(transport=SharedTransport())
    with pytest.raises(ValueError, match="MockTransport|custom transport"):
        WechatArticleProvider(transport=SharedTransport())


@pytest.mark.parametrize(
    "status,code,retryable",
    [(403, ErrorCode.HTTP_403, True), (404, ErrorCode.HTTP_404, False), (429, ErrorCode.HTTP_429, True),
     (503, ErrorCode.HTTP_5XX, True), (400, ErrorCode.HTTP_ERROR, False)],
)
def test_http_status_mapping(status, code, retryable) -> None:
    assert error_for_status(status) == (code, retryable)


@pytest.mark.asyncio
async def test_direct_download_streams_bytes_and_follows_validated_redirects() -> None:
    seen: list[str] = []
    requests: list[str] = []

    async def validate(url: str) -> None:
        seen.append(url)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, content=b"raw file", headers={"content-type": "application/pdf"})

    provider = DirectDownloadProvider(transport=httpx.MockTransport(handler), url_validator=validate)
    result = await provider.fetch("https://example.com/start")

    assert result.success is True
    assert result.payloads[0].data == b"raw file"
    assert result.payloads[0].mime_type == "application/pdf"
    assert result.redirect_chain == ["https://example.com/start"]
    assert seen == ["https://example.com/start", "https://example.com/final"]
    assert requests == ["https://example.com/start", "https://example.com/final"]


@pytest.mark.asyncio
async def test_direct_download_maps_error_and_size_limit() -> None:
    error_provider = DirectDownloadProvider(
        transport=httpx.MockTransport(lambda request: httpx.Response(429)), url_validator=allow_public_url
    )
    error = await error_provider.fetch("https://example.com/rate-limited")
    assert error.error_code == ErrorCode.HTTP_429
    assert error.retryable is True

    large_provider = DirectDownloadProvider(
        max_bytes=3,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"1234")),
        url_validator=allow_public_url,
    )
    large = await large_provider.fetch("https://example.com/large")
    assert large.error_code == ErrorCode.RESPONSE_TOO_LARGE


@pytest.mark.asyncio
async def test_direct_download_rejects_content_length_before_reading_body() -> None:
    class ExplodingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("oversized response body must not be read")
            yield b""  # pragma: no cover

    provider = DirectDownloadProvider(
        max_bytes=3,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-length": "4"}, stream=ExplodingStream())
        ),
        url_validator=allow_public_url,
    )
    result = await provider.fetch("https://example.com/large")
    assert result.error_code == ErrorCode.RESPONSE_TOO_LARGE


@pytest.mark.asyncio
async def test_direct_download_pins_connection_to_the_validated_address(monkeypatch) -> None:
    import clipdeck.acquisition.providers as provider_module

    async def resolve(url: str):
        assert url == "https://public.example/file"
        return ("8.8.8.8",)

    monkeypatch.setattr(provider_module, "resolve_public_http_url", resolve)
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["host"] = request.headers["host"]
        observed["sni"] = request.extensions["sni_hostname"]
        return httpx.Response(200, content=b"file", headers={"content-type": "application/pdf"})

    provider = DirectDownloadProvider(transport=httpx.MockTransport(handler))
    result = await provider.fetch("https://public.example/file")

    assert result.success is True
    assert observed == {
        "url": "https://8.8.8.8/file",
        "host": "public.example",
        "sni": "public.example",
    }
    assert result.final_url == "https://public.example/file"


@pytest.mark.asyncio
async def test_pinned_redirects_do_not_share_cookies_across_logical_origins(monkeypatch) -> None:
    import clipdeck.acquisition.providers as provider_module

    async def resolve(url: str):
        return ("8.8.8.8",)

    monkeypatch.setattr(provider_module, "resolve_public_http_url", resolve)
    seen_cookies: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cookies.append(request.headers.get("cookie"))
        if len(seen_cookies) == 1:
            return httpx.Response(
                302,
                headers={"location": "https://second.example/final", "set-cookie": "secret=first-only"},
            )
        return httpx.Response(200, content=b"file", headers={"content-type": "application/pdf"})

    result = await DirectDownloadProvider(transport=httpx.MockTransport(handler)).fetch(
        "https://first.example/start"
    )
    assert result.success is True
    assert seen_cookies == [None, None]


@pytest.mark.asyncio
async def test_pinned_redirects_preserve_same_origin_cookie(monkeypatch) -> None:
    import clipdeck.acquisition.providers as provider_module

    async def resolve(url: str):
        return ("8.8.8.8",)

    monkeypatch.setattr(provider_module, "resolve_public_http_url", resolve)
    seen_cookies: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cookies.append(request.headers.get("cookie"))
        if len(seen_cookies) == 1:
            return httpx.Response(302, headers={"location": "/final", "set-cookie": "session=needed"})
        return httpx.Response(200, content=b"file", headers={"content-type": "application/pdf"})

    result = await DirectDownloadProvider(transport=httpx.MockTransport(handler)).fetch(
        "https://same.example/start"
    )
    assert result.success is True
    assert seen_cookies == [None, "session=needed"]


@pytest.mark.asyncio
async def test_pinned_redirects_accept_domain_cookie_for_logical_host(monkeypatch) -> None:
    import clipdeck.acquisition.providers as provider_module

    async def resolve(url: str):
        return ("8.8.8.8",)

    monkeypatch.setattr(provider_module, "resolve_public_http_url", resolve)
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        if len(seen) == 1:
            return httpx.Response(
                302, headers={"location": "/final", "set-cookie": "domain_session=ok; Domain=same.example; Path=/"}
            )
        return httpx.Response(200, content=b"file", headers={"content-type": "application/pdf"})

    result = await DirectDownloadProvider(transport=httpx.MockTransport(handler)).fetch(
        "https://same.example/start"
    )
    assert result.success is True
    assert seen == [None, "domain_session=ok"]


@pytest.mark.asyncio
async def test_logical_cookie_respects_path_scope_and_deletion(monkeypatch) -> None:
    import clipdeck.acquisition.providers as provider_module

    async def resolve(url: str):
        return ("8.8.8.8",)

    monkeypatch.setattr(provider_module, "resolve_public_http_url", resolve)
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        logical_path = request.url.path
        seen.append((logical_path, request.headers.get("cookie")))
        if logical_path == "/private/start":
            return httpx.Response(
                302, headers={"location": "/public/set", "set-cookie": "scoped=private; Path=/private"}
            )
        if logical_path == "/public/set":
            return httpx.Response(
                302, headers={"location": "/public/delete", "set-cookie": "session=alive; Path=/"}
            )
        if logical_path == "/public/delete":
            return httpx.Response(
                302, headers={"location": "/public/final", "set-cookie": "session=; Max-Age=0; Path=/"}
            )
        return httpx.Response(200, content=b"file", headers={"content-type": "application/pdf"})

    result = await DirectDownloadProvider(transport=httpx.MockTransport(handler)).fetch(
        "https://same.example/private/start"
    )
    assert result.success is True
    assert seen == [
        ("/private/start", None),
        ("/public/set", None),
        ("/public/delete", "session=alive"),
        ("/public/final", None),
    ]


@pytest.mark.asyncio
async def test_wechat_streams_document_and_children_with_limits(monkeypatch) -> None:
    html = b'<div id="js_article"><div id="js_content"><p>article</p><img src="https://img.example/a.png"></div></div>'
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "mp.weixin.qq.com":
            return httpx.Response(200, content=html, headers={"content-type": "text/html"})
        return httpx.Response(200, content=b"1234", headers={"content-type": "image/png"})

    provider = WechatArticleProvider(
        max_document_bytes=len(html), max_child_bytes=3,
        transport=httpx.MockTransport(handler), url_validator=allow_public_url,
    )
    result = await provider.fetch("https://mp.weixin.qq.com/s/a")

    assert result.success is True
    assert result.payloads[0].data == html
    assert result.child_payloads == []
    assert result.warnings == ["child_too_large:https://img.example/a.png"]
    assert requests == ["https://mp.weixin.qq.com/s/a", "https://img.example/a.png"]


@pytest.mark.asyncio
async def test_wechat_rejects_oversized_main_document_from_content_length() -> None:
    class ExplodingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("oversized document must not be read")
            yield b""  # pragma: no cover

    provider = WechatArticleProvider(
        max_document_bytes=3,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-length": "4"}, stream=ExplodingStream())
        ),
        url_validator=allow_public_url,
    )
    result = await provider.fetch("https://mp.weixin.qq.com/s/a")
    assert result.error_code == ErrorCode.RESPONSE_TOO_LARGE


@pytest.mark.asyncio
async def test_wechat_child_redirect_is_validated_pinned_and_downloaded(monkeypatch) -> None:
    import clipdeck.acquisition.providers as provider_module

    html = b'<div id="js_article"><div id="js_content"><img src="https://img.example/start"></div></div>'
    validated: list[str] = []
    final_cookie: list[str | None] = []

    async def resolve(url: str):
        validated.append(url)
        return ("8.8.8.8",)

    monkeypatch.setattr(provider_module, "resolve_public_http_url", resolve)
    pinned_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pinned_hosts.append(request.url.host)
        logical_host = request.headers["host"]
        if logical_host == "mp.weixin.qq.com":
            return httpx.Response(200, content=html)
        if logical_host == "img.example":
            return httpx.Response(
                302,
                headers={"location": "https://cdn.example/final.png", "set-cookie": "source_only=secret"},
            )
        final_cookie.append(request.headers.get("cookie"))
        return httpx.Response(200, content=b"PNG", headers={"content-type": "image/png"})

    result = await WechatArticleProvider(transport=httpx.MockTransport(handler)).fetch(
        "https://mp.weixin.qq.com/s/a"
    )
    assert result.success is True
    assert result.child_payloads[0].data == b"PNG"
    assert result.child_payloads[0].original_url == "https://cdn.example/final.png"
    assert validated == [
        "https://mp.weixin.qq.com/s/a", "https://img.example/start", "https://cdn.example/final.png",
    ]
    assert pinned_hosts == ["8.8.8.8", "8.8.8.8", "8.8.8.8"]
    assert final_cookie == [None]


def test_wechat_validator_and_lazy_image_scan() -> None:
    provider = WechatArticleProvider()
    valid = '<div id="js_article"><div id="js_content"><img data-src="https://mmbiz.qpic.cn/a.jpg"><img src="https://mmbiz.qpic.cn/a.jpg"></div></div>'
    assert provider._validate(valid) == (ValidationStatus.VALID, None)
    assert provider._validate("内容已被发布者删除")[0] is ValidationStatus.DELETED
    assert provider._validate("访问过于频繁")[0] is ValidationStatus.BLOCKED
    assert provider._validate("<html>not an article</html>")[0] is ValidationStatus.INVALID
    assert provider._image_urls(valid, "https://mp.weixin.qq.com/s/a") == ["https://mmbiz.qpic.cn/a.jpg"]


def test_provider_resolver_keeps_future_routes_out_of_web_provider() -> None:
    resolver = ProviderResolver()
    assert resolver.resolve(ResourceType.WECHAT_ARTICLE) is resolver.wechat
    assert resolver.resolve(ResourceType.WEB_PAGE) is resolver.crawl4ai
    assert resolver.resolve(ResourceType.PODCAST) is resolver.direct


@pytest.mark.asyncio
async def test_crawl4ai_adapter_has_explicit_http_fallback() -> None:
    class FakeFallback:
        async def fetch(self, url: str):
            return ProviderFetchResult(success=True, requested_url=url, validation_status=ValidationStatus.VALID)

    def missing_runtime(**kwargs):
        raise RuntimeError("browser runtime missing")

    provider = Crawl4AIProvider(
        crawler_factory=missing_runtime,
        fallback=FakeFallback(),
        url_validator=allow_public_url,
    )
    result = await provider.fetch("https://example.com")
    assert result.success is True
    assert any("http_archive_fallback" in warning for warning in result.warnings)
