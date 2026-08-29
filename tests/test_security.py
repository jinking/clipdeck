import socket

import pytest

from clipdeck.acquisition.security import UnsafeTargetError, validate_public_http_url


@pytest.mark.asyncio
async def test_ssrf_guard_accepts_public_and_rejects_private(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    await validate_public_http_url("https://example.com/a")

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))])
    with pytest.raises(UnsafeTargetError, match="non-public"):
        await validate_public_http_url("http://example.com/a")


@pytest.mark.asyncio
async def test_ssrf_guard_rejects_credentials_and_bad_scheme() -> None:
    with pytest.raises(UnsafeTargetError, match="credentials"):
        await validate_public_http_url("https://user:secret@example.com")
    with pytest.raises(UnsafeTargetError, match="HTTP"):
        await validate_public_http_url("file:///etc/passwd")


@pytest.mark.asyncio
async def test_ssrf_guard_allows_proxy_fake_ip_only_when_explicitly_enabled(monkeypatch) -> None:
    monkeypatch.delenv("SCI_ALLOW_PROXY_FAKE_IP", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.84", 443))])
    with pytest.raises(UnsafeTargetError):
        await validate_public_http_url("https://mp.weixin.qq.com")

    monkeypatch.setenv("SCI_ALLOW_PROXY_FAKE_IP", "true")
    await validate_public_http_url("https://mp.weixin.qq.com")
