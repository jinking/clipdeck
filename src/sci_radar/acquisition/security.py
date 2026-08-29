from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlsplit


class UnsafeTargetError(ValueError):
    pass


FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


async def resolve_public_http_url(url: str, *, allow_proxy_fake_ip: bool | None = None) -> tuple[str, ...]:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise UnsafeTargetError("Only absolute HTTP(S) URLs are allowed")
    if parts.username or parts.password:
        raise UnsafeTargetError("URLs with embedded credentials are not allowed")

    def resolve() -> set[str]:
        return {item[4][0] for item in socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)}

    addresses = await asyncio.to_thread(resolve)
    if allow_proxy_fake_ip is None:
        allow_proxy_fake_ip = os.getenv("SCI_ALLOW_PROXY_FAKE_IP", "").lower() in {"1", "true", "yes"}
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if allow_proxy_fake_ip and ip in FAKE_IP_NETWORK:
            continue
        if not ip.is_global:
            raise UnsafeTargetError(f"Target resolves to a non-public address: {address}")
    return tuple(sorted(addresses, key=lambda item: (ipaddress.ip_address(item).version, item)))


async def validate_public_http_url(url: str, *, allow_proxy_fake_ip: bool | None = None) -> None:
    await resolve_public_http_url(url, allow_proxy_fake_ip=allow_proxy_fake_ip)
