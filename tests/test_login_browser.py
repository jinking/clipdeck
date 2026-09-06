from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clipdeck.acquisition.domain import (
    AcquisitionInput,
    AttemptStatus,
    BlobRole,
    ProviderFetchResult,
    ProviderName,
    ProviderPayload,
    ResourceType,
    SourceKind,
    TaskStatus,
    ValidationStatus,
)
from clipdeck.acquisition.providers import LoginBrowserProvider, validate_cdp_url
from clipdeck.acquisition.repository import SQLiteRepository
from clipdeck.acquisition.service import AcquisitionService
from clipdeck.acquisition.storage import LocalBlobStore
from clipdeck.acquisition.truncation import TruncationDetector

ZHIHU_TRUNCATED_HTML = (
    "<html><body><div class='RichText'>因为剧情总需要猴子有一个旗鼓相当的对手。"
    "……在杂剧中的作用是充当旁白。</div>"
    "<script id='js-initialData'>{\"contentNeedTruncated\":true}</script>"
    "<button class='ContentItem-expandButton'>阅读全文</button></body></html>"
)
ZHIHU_FULL_HTML = (
    "<html><body><div class='RichText'>因为剧情总需要猴子有一个旗鼓相当的对手。"
    "……这就是二郎神以反派登场的完整答案。"
    "以下为拟真长度补充正文段落，" * 20 + "确保通过采集质量门禁。</div>"
    "<script id='js-initialData'>{\"contentNeedTruncated\":false}</script></body></html>"
)


# --------------------------------------------------------------------------
# Truncation detector
# --------------------------------------------------------------------------

def test_domain_marker_detected_for_configured_host() -> None:
    detector = TruncationDetector({"www.zhihu.com": ['"contentNeedTruncated":true']})
    signal = detector.detect("https://www.zhihu.com/question/1/answer/2", ZHIHU_TRUNCATED_HTML)
    assert signal == {"marker": '"contentNeedTruncated":true', "scope": "domain", "domain": "www.zhihu.com"}


def test_generic_marker_detected_on_any_domain() -> None:
    detector = TruncationDetector({})
    html = "<html><body>正文开头……开通会员后查看全文</body></html>"
    signal = detector.detect("https://example.com/post", html)
    assert signal is not None and signal["scope"] == "generic"


def test_clean_page_is_not_flagged() -> None:
    detector = TruncationDetector({"www.zhihu.com": ['"contentNeedTruncated":true']})
    assert detector.detect("https://www.zhihu.com/question/1/answer/2", ZHIHU_FULL_HTML) is None
    # A page that merely mentions 登录 in navigation must not trip the detector.
    assert detector.detect("https://www.tgb.cn/a/x", "<html><body><a>登录/注册</a>完整正文</body></html>") is None


def test_domain_markers_take_precedence_over_generic() -> None:
    detector = TruncationDetector({"www.zhihu.com": ["ContentItem-expandButton"]})
    signal = detector.detect("https://www.zhihu.com/q/1/a/2", ZHIHU_TRUNCATED_HTML)
    assert signal["marker"] == "ContentItem-expandButton" and signal["scope"] == "domain"


def test_subdomain_inherits_parent_domain_truncation_marker() -> None:
    detector = TruncationDetector({"zhihu.com": ['"contentNeedTruncated":true']})
    signal = detector.detect("https://zhuanlan.zhihu.com/p/123456", ZHIHU_TRUNCATED_HTML)
    assert signal is not None
    assert signal["marker"] == '"contentNeedTruncated":true'
    assert signal["scope"] == "domain"
    assert signal["domain"] == "zhihu.com"


# --------------------------------------------------------------------------
# CDP endpoint policy
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9222",
    "http://localhost:9222",
    "http://[::1]:9222",
])
def test_loopback_cdp_urls_accepted(url: str) -> None:
    assert validate_cdp_url(url) == url


@pytest.mark.parametrize("url", [
    "http://evil.example.com:9222",
    "http://10.0.0.5:9222",
    "http://user:pass@127.0.0.1:9222",
    "ws://127.0.0.1:9222",
    "not a url",
])
def test_non_loopback_or_credential_cdp_urls_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        validate_cdp_url(url)


# --------------------------------------------------------------------------
# LoginBrowserProvider (injected crawler; no real browser needed)
# --------------------------------------------------------------------------

@dataclass
class FakeCrawler:
    results: list[Any]
    started: int = 0
    calls: list[tuple[str, Any]] = field(default_factory=list)

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        pass

    async def arun(self, *, url: str, config: Any) -> Any:
        self.calls.append((url, config))
        return self.results.pop(0)


class FakeFallback:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(self, url: str, *, capture_screenshot: bool = False) -> ProviderFetchResult:
        del capture_screenshot
        self.calls.append(url)
        return ProviderFetchResult(success=True, validation_status=ValidationStatus.VALID)


async def _allow(url: str) -> None:
    del url


def _crawl_result(url: str, html: str) -> SimpleNamespace:
    return SimpleNamespace(
        success=True, url=url, html=html, status_code=200,
        response_headers={"content-type": "text/html"}, screenshot=None, mhtml=None,
    )


def _login_provider(crawler: FakeCrawler, fallback: FakeFallback) -> LoginBrowserProvider:
    return LoginBrowserProvider(
        cdp_url="http://127.0.0.1:9223",
        crawler_factory=lambda **kwargs: crawler,
        browser_config_factory=lambda **kwargs: kwargs,
        run_config_factory=lambda **kwargs: kwargs,
        cache_mode="BYPASS",
        fallback=fallback,  # type: ignore[arg-type]
        url_validator=_allow,
    )


@pytest.mark.asyncio
async def test_login_provider_attaches_with_cdp_and_reports_adapter() -> None:
    crawler = FakeCrawler(results=[_crawl_result("https://www.zhihu.com/q/1/a/2", ZHIHU_FULL_HTML)])
    provider = _login_provider(crawler, FakeFallback())
    assert await provider.start() is True
    # The browser config handed to Crawl4AI must attach, never kill the user's browser.
    result = await provider.fetch("https://www.zhihu.com/question/1/answer/2")
    assert result.success is True
    assert result.provider_meta["adapter"] == "login_browser"
    assert result.payloads[0].role is BlobRole.RENDERED_HTML


@pytest.mark.asyncio
async def test_login_provider_builds_loopback_attach_config() -> None:
    kwargs = LoginBrowserProvider(cdp_url="http://localhost:9222")._build_browser_kwargs()
    assert kwargs["cdp_url"] == "http://localhost:9222"
    assert kwargs["cdp_cleanup_on_close"] is False
    assert "headless" not in kwargs and "user_agent_mode" not in kwargs


@pytest.mark.asyncio
async def test_login_provider_never_falls_back_to_anonymous_http() -> None:
    def failing_factory(**kwargs: Any) -> FakeCrawler:
        raise RuntimeError("no browser on that port")

    fallback = FakeFallback()
    provider = LoginBrowserProvider(
        cdp_url="http://127.0.0.1:9223",
        crawler_factory=failing_factory,
        fallback=fallback,  # type: ignore[arg-type]
        url_validator=_allow,
    )
    result = await provider.fetch("https://www.zhihu.com/question/1/answer/2")
    assert result.success is False
    assert "login_browser unavailable" in (result.error_message or "")
    assert fallback.calls == []


@pytest.mark.asyncio
async def test_login_provider_retries_start_after_transient_failure() -> None:
    crawler = FakeCrawler(results=[_crawl_result(
        "https://example.com",
        "<html><body><p>" + "login browser retry fixture content. " * 12 + "</p></body></html>",
    )])
    attempts = {"n": 0}

    def flaky_factory(**kwargs: Any) -> FakeCrawler:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("chrome not restarted yet")
        return crawler

    provider = LoginBrowserProvider(
        cdp_url="http://127.0.0.1:9223",
        crawler_factory=flaky_factory,
        browser_config_factory=lambda **kwargs: kwargs,
        run_config_factory=lambda **kwargs: kwargs,
        cache_mode="BYPASS",
        url_validator=_allow,
    )
    first = await provider.fetch("https://example.com")
    assert first.success is False
    second = await provider.fetch("https://example.com")
    assert second.success is True


@pytest.mark.asyncio
async def test_login_provider_recovers_when_browser_disconnects_mid_session() -> None:
    class DisconnectingCrawler:
        def __init__(self) -> None:
            self.closed = False

        async def start(self) -> None:
            pass

        async def close(self) -> None:
            self.closed = True

        async def arun(self, *, url: str, config: Any) -> Any:
            del config
            if "disconnect" in url:
                raise RuntimeError("cdp connection dropped")
            return _crawl_result(
                url,
                "<html><body><p>" + "browser fixture content after reconnect. " * 12 + "</p></body></html>",
            )

    provider = LoginBrowserProvider(
        cdp_url="http://127.0.0.1:9223",
        crawler_factory=lambda **kwargs: DisconnectingCrawler(),
        browser_config_factory=lambda **kwargs: kwargs,
        run_config_factory=lambda **kwargs: kwargs,
        cache_mode="BYPASS",
        url_validator=_allow,
    )

    r1 = await provider.fetch("https://example.com/page1")
    assert r1.success is True

    # Mid-session disconnection
    r2 = await provider.fetch("https://example.com/disconnect")
    assert r2.success is False
    assert provider._crawler is None
    assert provider._started is False

    # Should cleanly re-attach on the next fetch without restart
    r3 = await provider.fetch("https://example.com/page2")
    assert r3.success is True



# --------------------------------------------------------------------------
# Service upgrade flow
# --------------------------------------------------------------------------

@dataclass
class StubProvider:
    html: str
    adapter: str = "crawl4ai"
    success: bool = True
    error_code: str | None = None
    calls: list[str] = field(default_factory=list)

    async def fetch(self, url: str, *, capture_screenshot: bool = False) -> ProviderFetchResult:
        del capture_screenshot
        self.calls.append(url)
        if not self.success:
            return ProviderFetchResult(
                success=False, requested_url=url, final_url=url,
                error_code=self.error_code or "PROVIDER_ERROR", retryable=False,
                provider_meta={"adapter": self.adapter},
            )
        return ProviderFetchResult(
            success=True, requested_url=url, final_url=url,
            validation_status=ValidationStatus.VALID,
            payloads=[ProviderPayload(
                data=self.html.encode("utf-8"), role=BlobRole.RENDERED_HTML,
                mime_type="text/html; charset=utf-8", original_url=url, is_primary=True,
            )],
            provider_meta={"adapter": self.adapter},
        )


class StubResolver:
    def __init__(self, provider: StubProvider) -> None:
        self.provider = provider

    def resolve(self, resource_type: ResourceType) -> StubProvider:
        return self.provider


ZHIHU_URL = "https://www.zhihu.com/question/664893150/answer/3604657357"


@pytest.fixture
async def factory(tmp_path):
    repository = SQLiteRepository(tmp_path / "acquisition.db")
    await repository.initialize()
    blob_store = LocalBlobStore(tmp_path / "data")

    def make(
        *,
        primary: StubProvider,
        login: StubProvider | None = None,
        spider: StubProvider | None = None,
    ) -> AcquisitionService:
        return AcquisitionService(
            repository=repository, blob_store=blob_store, resolver=StubResolver(primary),
            login_provider=login,
            spider_provider=spider,
            truncation_detector=TruncationDetector({"www.zhihu.com": ['"contentNeedTruncated":true']}),
        )

    yield make
    await repository.close()


async def _execute_zhihu(service: AcquisitionService):
    task = await service.submit(AcquisitionInput(source_kind=SourceKind.URL, url=ZHIHU_URL))
    asset = await service.execute(task.task_id)
    assert asset is not None
    return task, asset


@pytest.mark.asyncio
async def test_spider_bypass_upgrades_truncated_page_without_login_browser(factory) -> None:
    primary = StubProvider(html=ZHIHU_TRUNCATED_HTML)
    spider = StubProvider(html=ZHIHU_FULL_HTML, adapter="spider_bypass")
    service = factory(primary=primary, login=None, spider=spider)

    task, final = await _execute_zhihu(service)

    assert spider.calls == [ZHIHU_URL]
    assert final.version_no == 2
    assert final.provider_name is ProviderName.SPIDER_BYPASS
    assert final.acquisition_status is TaskStatus.SUCCESS
    assert "spider_upgraded" in final.warnings


@pytest.mark.asyncio
async def test_spider_bypass_failure_falls_back_to_login_browser(factory) -> None:
    primary = StubProvider(html=ZHIHU_TRUNCATED_HTML)
    spider = StubProvider(html=ZHIHU_TRUNCATED_HTML, adapter="spider_bypass")
    login = StubProvider(html=ZHIHU_FULL_HTML, adapter="login_browser")
    service = factory(primary=primary, login=login, spider=spider)

    task, final = await _execute_zhihu(service)

    assert spider.calls == [ZHIHU_URL]
    assert login.calls == [ZHIHU_URL]
    assert final.version_no == 2
    assert final.provider_name is ProviderName.LOGIN_BROWSER
    assert "login_upgraded" in final.warnings


@pytest.mark.asyncio
async def test_truncated_capture_is_upgraded_to_logged_in_version(factory) -> None:
    primary = StubProvider(html=ZHIHU_TRUNCATED_HTML)
    login = StubProvider(html=ZHIHU_FULL_HTML, adapter="login_browser")
    service = factory(primary=primary, login=login, spider=None)

    task, final = await _execute_zhihu(service)

    assert login.calls == [ZHIHU_URL]
    assert final.version_no == 2
    assert final.provider_name is ProviderName.LOGIN_BROWSER
    assert final.acquisition_status is TaskStatus.SUCCESS
    assert "login_upgraded" in final.warnings

    assets = await service.repository.list_assets(limit=10)
    versions = sorted(a.version_no for a in assets)
    assert versions == [1, 2]
    v1 = next(a for a in assets if a.version_no == 1)
    assert v1.provider_name is ProviderName.CRAWL4AI
    assert any(w.startswith("login_truncated:") for w in v1.warnings)
    assert v1.provider_meta["truncation"]["marker"] == '"contentNeedTruncated":true'
    assert final.previous_asset_id == v1.asset_id
    assert final.changed_from_previous is True
    stored_task = await service.repository.get_task(task.task_id)
    assert stored_task is not None and stored_task.latest_asset_id == final.asset_id


@pytest.mark.asyncio
async def test_failed_upgrade_keeps_truncated_asset_with_warnings(factory) -> None:
    primary = StubProvider(html=ZHIHU_TRUNCATED_HTML)
    login = StubProvider(html="", success=False, error_code="CONNECTION_ERROR")
    service = factory(primary=primary, login=login, spider=None)

    task, asset = await _execute_zhihu(service)

    assert asset.version_no == 1
    assert asset.provider_name is ProviderName.CRAWL4AI
    assert any(w.startswith("login_truncated:") for w in asset.warnings)
    assert "login_upgrade_failed:CONNECTION_ERROR" in asset.warnings
    assets = await service.repository.list_assets(limit=10)
    assert len(assets) == 1

    attempts = await service.repository.list_attempts(task.task_id)
    assert len(attempts) == 2
    assert attempts[0].status == AttemptStatus.SUCCESS
    assert attempts[1].status == AttemptStatus.PERMANENT_FAILURE
    assert attempts[1].provider_name == ProviderName.LOGIN_BROWSER
    assert attempts[1].error_code == "login_upgrade_failed:CONNECTION_ERROR"


@pytest.mark.asyncio
async def test_still_truncated_upgrade_is_not_archived(factory) -> None:
    primary = StubProvider(html=ZHIHU_TRUNCATED_HTML)
    login = StubProvider(html=ZHIHU_TRUNCATED_HTML, adapter="login_browser")
    service = factory(primary=primary, login=login, spider=None)

    _, asset = await _execute_zhihu(service)

    assert asset.version_no == 1
    assert "login_upgrade_still_truncated" in asset.warnings
    assets = await service.repository.list_assets(limit=10)
    assert len(assets) == 1


@pytest.mark.asyncio
async def test_clean_capture_skips_login_provider_entirely(factory) -> None:
    primary = StubProvider(html=ZHIHU_FULL_HTML)
    login = StubProvider(html="unused", adapter="login_browser")
    service = factory(primary=primary, login=login, spider=None)

    _, asset = await _execute_zhihu(service)

    assert login.calls == []
    assert asset.version_no == 1
    assert not any("login_" in w for w in asset.warnings)


@pytest.mark.asyncio
async def test_without_login_provider_truncation_is_not_even_checked(factory) -> None:
    primary = StubProvider(html=ZHIHU_TRUNCATED_HTML)
    service = factory(primary=primary, login=None, spider=None)

    _, asset = await _execute_zhihu(service)

    assert asset.version_no == 1
    assert "truncation" not in asset.provider_meta
    assert not any("login_" in w for w in asset.warnings)


@pytest.mark.asyncio
async def test_truncation_detection_uses_redirected_final_url(factory) -> None:
    short_url = "https://t.cn/xyz123"
    primary = StubProvider(html=ZHIHU_TRUNCATED_HTML)

    async def redirect_fetch(url: str, *, capture_screenshot: bool = False) -> ProviderFetchResult:
        res = await StubProvider.fetch(primary, url, capture_screenshot=capture_screenshot)
        res.final_url = ZHIHU_URL
        return res

    primary.fetch = redirect_fetch  # type: ignore[assignment]
    spider = StubProvider(html=ZHIHU_FULL_HTML, adapter="spider_bypass")
    service = factory(primary=primary, login=None, spider=spider)

    task = await service.submit(AcquisitionInput(source_kind=SourceKind.URL, url=short_url))
    final = await service.execute(task.task_id)
    assert final is not None
    assert final.version_no == 2
    assert final.provider_name is ProviderName.SPIDER_BYPASS
    assert "spider_upgraded" in final.warnings


# --------------------------------------------------------------------------
# Config plumbing
# --------------------------------------------------------------------------

def test_site_profiles_loads_truncation_markers(tmp_path: Path, monkeypatch) -> None:
    from clipdeck.ingestion import site_profiles

    config = tmp_path / "profiles.json"
    config.write_text(
        '{"truncation_markers": {"www.zhihu.com": ["contentNeedTruncated", ""]}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLIPDECK_SITE_PROFILES", str(config))
    profiles = site_profiles.reload_profiles()
    assert profiles["truncation_markers"] == {"www.zhihu.com": ["contentNeedTruncated"]}
    site_profiles.reload_profiles()
