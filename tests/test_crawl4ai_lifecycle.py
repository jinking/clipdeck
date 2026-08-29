from __future__ import annotations

import base64
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from clipdeck.acquisition.domain import (
    BlobRole,
    ErrorCode,
    ProviderFetchResult,
    ValidationStatus,
)
from clipdeck.acquisition.providers import Crawl4AIProvider
from clipdeck.acquisition.security import UnsafeTargetError


@dataclass
class FakeCrawler:
    results: list[Any]
    started: int = 0
    closed: int = 0
    calls: list[tuple[str, Any]] = field(default_factory=list)

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        self.closed += 1

    async def arun(self, *, url: str, config: Any) -> Any:
        self.calls.append((url, config))
        return self.results.pop(0)


class FakeFallback:
    def __init__(self, result: ProviderFetchResult | None = None) -> None:
        self.result = result or ProviderFetchResult(
            success=True,
            requested_url="https://example.com",
            final_url="https://example.com",
            validation_status=ValidationStatus.VALID,
        )
        self.calls: list[str] = []

    async def fetch(self, url: str, *, capture_screenshot: bool = False) -> ProviderFetchResult:
        del capture_screenshot
        self.calls.append(url)
        return self.result


def fake_browser_config(**kwargs: Any) -> dict[str, Any]:
    return kwargs


def fake_run_config(**kwargs: Any) -> dict[str, Any]:
    return kwargs


@pytest.mark.asyncio
async def test_crawl4ai_reuses_started_crawler_and_closes_it() -> None:
    crawler = FakeCrawler(
        results=[
            SimpleNamespace(
                success=True,
                url="https://example.com/one",
                html="<html>one</html>",
                status_code=200,
                response_headers={"content-type": "text/html"},
                screenshot=None,
                mhtml=None,
            ),
            SimpleNamespace(
                success=True,
                url="https://example.com/two",
                html="<html>two</html>",
                status_code=200,
                response_headers={"content-type": "text/html"},
                screenshot=None,
                mhtml=None,
            ),
        ]
    )
    factory_calls: list[dict[str, Any]] = []

    def crawler_factory(**kwargs: Any) -> FakeCrawler:
        factory_calls.append(kwargs)
        return crawler

    provider = Crawl4AIProvider(
        crawler_factory=crawler_factory,
        browser_config_factory=fake_browser_config,
        run_config_factory=fake_run_config,
        cache_mode="BYPASS",
        url_validator=lambda url: _allow(url),
    )

    await provider.start()
    first = await provider.fetch("https://example.com/one")
    second = await provider.fetch("https://example.com/two")
    await provider.close()

    assert len(factory_calls) == 1
    assert crawler.started == 1
    assert crawler.closed == 1
    assert len(crawler.calls) == 2
    assert first.success is True
    assert second.success is True
    assert [payload.role for payload in first.payloads] == [BlobRole.RENDERED_HTML]
    assert crawler.calls[0][1]["cache_mode"] == "BYPASS"


@pytest.mark.asyncio
async def test_crawl4ai_saves_rendered_html_screenshot_optional_mhtml_and_headers() -> None:
    screenshot = base64.b64encode(b"png-bytes").decode("ascii")
    crawler = FakeCrawler(
        results=[
            SimpleNamespace(
                success=True,
                url="https://example.com/final",
                html="<html>rendered</html>",
                status_code=203,
                response_headers={"content-type": "text/html; charset=utf-8", "etag": "abc"},
                screenshot=screenshot,
                mhtml="From: mhtml\n\n<html>snapshot</html>",
                redirected_url="https://example.com/final",
            )
        ]
    )
    provider = Crawl4AIProvider(
        crawler_factory=lambda **kwargs: crawler,
        browser_config_factory=fake_browser_config,
        run_config_factory=fake_run_config,
        cache_mode="BYPASS",
        capture_mhtml=True,
        url_validator=lambda url: _allow(url),
    )

    result = await provider.fetch("https://example.com/start", capture_screenshot=True)

    assert result.success is True
    assert result.final_url == "https://example.com/final"
    assert result.http_status == 203
    assert result.response_headers["etag"] == "abc"
    assert [payload.role for payload in result.payloads] == [
        BlobRole.RENDERED_HTML,
        BlobRole.SCREENSHOT,
        BlobRole.MHTML,
    ]
    assert result.payloads[0].data == b"<html>rendered</html>"
    assert result.payloads[1].data == b"png-bytes"
    assert result.payloads[2].data.startswith(b"From: mhtml")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ImportError("crawl4ai missing"), RuntimeError("browser missing")])
async def test_crawl4ai_runtime_or_browser_initialization_failure_falls_back(failure: Exception) -> None:
    fallback = FakeFallback()

    def failing_factory(**kwargs: Any) -> Any:
        del kwargs
        raise failure

    provider = Crawl4AIProvider(
        crawler_factory=failing_factory,
        browser_config_factory=fake_browser_config,
        run_config_factory=fake_run_config,
        cache_mode="BYPASS",
        fallback=fallback,
        url_validator=lambda url: _allow(url),
    )

    result = await provider.fetch("https://example.com")

    assert result.success is True
    assert fallback.calls == ["https://example.com"]
    assert any("http_archive_fallback" in warning for warning in result.warnings)
    assert result.provider_meta["adapter"] == "crawl4ai_fallback"


@pytest.mark.asyncio
async def test_crawl4ai_converts_ssrf_validation_error_to_blocked_result() -> None:
    fallback = FakeFallback()

    async def reject(url: str) -> None:
        del url
        raise UnsafeTargetError("private target")

    provider = Crawl4AIProvider(
        crawler_factory=lambda **kwargs: pytest.fail("crawler must not start for unsafe target"),
        fallback=fallback,
        url_validator=reject,
    )

    result = await provider.fetch("https://example.com")

    assert result.success is False
    assert result.error_code == ErrorCode.UNSAFE_TARGET
    assert result.validation_status is ValidationStatus.BLOCKED
    assert fallback.calls == []


async def _allow(url: str) -> None:
    del url


@pytest.mark.asyncio
async def test_crawl4ai_installs_guard_that_blocks_private_subresources() -> None:
    hooks: dict[str, Any] = {}

    class Strategy:
        def set_hook(self, name: str, callback: Any) -> None:
            hooks[name] = callback

    crawler = FakeCrawler(results=[])
    crawler.crawler_strategy = Strategy()

    async def validator(url: str) -> None:
        if "127.0.0.1" in url:
            raise UnsafeTargetError("private target")

    provider = Crawl4AIProvider(
        crawler_factory=lambda **kwargs: crawler,
        browser_config_factory=fake_browser_config,
        run_config_factory=fake_run_config,
        url_validator=validator,
    )
    provider._uses_default_url_validator = True
    assert await provider.start() is True

    class Page:
        async def route(self, pattern: str, callback: Any) -> None:
            assert pattern == "**/*"
            self.callback = callback

    class Route:
        aborted = False
        continued = False

        async def abort(self, reason: str) -> None:
            self.aborted = reason == "blockedbyclient"

        async def continue_(self) -> None:
            self.continued = True

    page = Page()
    await hooks["on_page_context_created"](page, object())
    route = Route()
    await page.callback(route, SimpleNamespace(url="http://127.0.0.1/metadata"))
    assert route.aborted is True
    assert route.continued is False
    await provider.close()


@pytest.mark.asyncio
async def test_crawl4ai_guard_rejects_private_redirect_before_browser_follows() -> None:
    hooks: dict[str, Any] = {}

    class Strategy:
        def set_hook(self, name: str, callback: Any) -> None:
            hooks[name] = callback

    crawler = FakeCrawler(results=[])
    crawler.crawler_strategy = Strategy()

    async def validator(url: str) -> None:
        if "127.0.0.1" in url:
            raise UnsafeTargetError("private target")

    provider = Crawl4AIProvider(
        crawler_factory=lambda **kwargs: crawler,
        browser_config_factory=fake_browser_config,
        run_config_factory=fake_run_config,
        url_validator=validator,
    )
    provider._uses_default_url_validator = True
    assert await provider.start() is True

    class Page:
        async def route(self, pattern: str, callback: Any) -> None:
            self.callback = callback

    class Route:
        aborted = False
        fulfilled = False

        async def fetch(self, *, max_redirects: int):
            assert max_redirects == 0
            return SimpleNamespace(
                status=302,
                headers={"location": "http://127.0.0.1/metadata"},
            )

        async def fulfill(self, *, response: Any) -> None:
            del response
            self.fulfilled = True

        async def abort(self, reason: str) -> None:
            self.aborted = reason == "blockedbyclient"

    page = Page()
    await hooks["on_page_context_created"](page, object())
    route = Route()
    await page.callback(route, SimpleNamespace(url="https://example.com/start"))
    assert route.aborted is True
    assert route.fulfilled is False
    await provider.close()
