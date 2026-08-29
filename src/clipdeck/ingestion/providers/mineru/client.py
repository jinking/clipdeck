from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit
import asyncio
import os
import random
import tempfile
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, SecretStr, field_validator

from clipdeck.ingestion.providers.mineru.schemas import BatchResult, FileBatch


class MinerUSettings(BaseModel):
    base_url: str = "https://mineru.net"
    token: SecretStr
    timeout_seconds: float = Field(default=180.0, gt=0)
    max_result_zip_bytes: int = 500 * 1024 * 1024
    request_retries: int = 3
    upload_retries: int = 2
    result_host_allowlist: set[str] = Field(
        default_factory=lambda: {
            "mineru.net",
            "download.mineru.test",
            "cdn-mineru.openxlab.org.cn",
            "openxlab.org.cn",
            "aliyuncs.com",
            "volces.com",
        }
    )

    @field_validator("base_url")
    @classmethod
    def require_https_base_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("MinerU base_url must be an HTTPS origin without credentials")
        return value.rstrip("/")


class MinerUHTTPError(RuntimeError):
    def __init__(self, operation: str, status_code: int | None = None):
        detail = f" status={status_code}" if status_code is not None else ""
        super().__init__(f"MinerU {operation} request failed{detail}")
        self.operation = operation
        self.status_code = status_code


class MinerUClient:
    def __init__(
        self,
        *,
        settings: MinerUSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.token.get_secret_value()}"}

    def _client_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            timeout=self.settings.timeout_seconds,
            connect=30.0,
            read=self.settings.timeout_seconds,
            write=self.settings.timeout_seconds,
            pool=30.0,
        )

    async def create_file_batch(
        self,
        *,
        files: list[dict[str, Any]],
        model_version: str = "vlm",
        enable_formula: bool = True,
        enable_table: bool = True,
        language: str = "en",
    ) -> FileBatch:
        payload = {
            "files": files,
            "model_version": model_version,
            "enable_formula": enable_formula,
            "enable_table": enable_table,
            "language": language,
        }
        response = await self._request("POST", "/api/v4/file-urls/batch", json=payload)
        data = self._provider_data(response.json())
        return FileBatch.model_validate(data)

    async def upload_file(self, upload_url: str, data: bytes) -> None:
        self._validate_https_url(upload_url)
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self._client_timeout(),
            trust_env=False,
        ) as client:
            for attempt in range(self.settings.upload_retries):
                try:
                    response = await client.put(upload_url, content=data, headers={})
                    response.raise_for_status()
                    return
                except httpx.HTTPStatusError as exc:
                    if not _retryable_status(exc.response.status_code) or attempt + 1 >= self.settings.upload_retries:
                        raise MinerUHTTPError("upload", exc.response.status_code) from None
                except httpx.RequestError:
                    if attempt + 1 >= self.settings.upload_retries:
                        raise MinerUHTTPError("upload") from None
                await _retry_delay(attempt)

    async def get_batch_result(self, batch_id: str) -> BatchResult:
        response = await self._request("GET", f"/api/v4/extract-results/batch/{batch_id}")
        data = self._provider_data(response.json())
        if "state" in data:
            return BatchResult.model_validate(data)
        results = data.get("extract_result") or data.get("extract_results") or []
        first = results[0] if results else {}
        return BatchResult(
            batch_id=data.get("batch_id", batch_id),
            state=first.get("state", "pending"),
            full_zip_url=first.get("full_zip_url"),
            error_code=first.get("err_code") or first.get("error_code"),
            error_message=first.get("err_msg") or first.get("error_message"),
        )

    async def download_result_zip(self, result_url: str) -> bytes:
        fd, temporary = tempfile.mkstemp(prefix="mineru-result-", suffix=".zip")
        os.close(fd)
        path = Path(temporary)
        try:
            await self.download_result_zip_to_path(result_url, path)
            return await asyncio.to_thread(path.read_bytes)
        finally:
            path.unlink(missing_ok=True)

    async def download_result_zip_to_path(self, result_url: str, destination: str | Path) -> Path:
        self._validate_result_url(result_url)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f".{destination.name}.partial")
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self._client_timeout(),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                for attempt in range(self.settings.request_retries):
                    try:
                        async with client.stream("GET", result_url) as response:
                            response.raise_for_status()
                            content_length = response.headers.get("content-length")
                            if content_length and int(content_length) > self.settings.max_result_zip_bytes:
                                raise ValueError("MinerU result archive exceeds size limit")
                            total = 0
                            with partial.open("wb") as handle:
                                async for chunk in response.aiter_bytes():
                                    total += len(chunk)
                                    if total > self.settings.max_result_zip_bytes:
                                        raise ValueError("MinerU result archive exceeds size limit")
                                    handle.write(chunk)
                                handle.flush()
                                os.fsync(handle.fileno())
                            os.replace(partial, destination)
                            return destination
                    except httpx.HTTPStatusError as exc:
                        if not _retryable_status(exc.response.status_code) or attempt + 1 >= self.settings.request_retries:
                            raise MinerUHTTPError("result download", exc.response.status_code) from None
                    except httpx.RequestError:
                        if attempt + 1 >= self.settings.request_retries:
                            raise MinerUHTTPError("result download") from None
                    partial.unlink(missing_ok=True)
                    await _retry_delay(attempt)
            raise MinerUHTTPError("result download")
        finally:
            partial.unlink(missing_ok=True)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(
            base_url=self.settings.base_url,
            transport=self.transport,
            timeout=self._client_timeout(),
            headers=self._auth_headers(),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for attempt in range(self.settings.request_retries):
                try:
                    response = await client.request(method, path, **kwargs)
                    response.raise_for_status()
                    return response
                except httpx.HTTPStatusError as exc:
                    if not _retryable_status(exc.response.status_code) or attempt + 1 >= self.settings.request_retries:
                        raise MinerUHTTPError("API", exc.response.status_code) from None
                except httpx.RequestError:
                    if attempt + 1 >= self.settings.request_retries:
                        raise MinerUHTTPError("API") from None
                await _retry_delay(attempt)
        raise MinerUHTTPError("API")

    @staticmethod
    def _provider_data(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("code") not in {0, "0", None}:
            raise ValueError(f"MinerU request failed with code {payload.get('code')}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("MinerU response is missing data")
        return data

    @staticmethod
    def _validate_https_url(url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("Signed URL must use https and include a host")

    def _validate_result_url(self, url: str) -> None:
        self._validate_https_url(url)
        host = (urlsplit(url).hostname or "").lower()
        allowed = self.settings.result_host_allowlist
        if host not in allowed and not any(host.endswith(f".{item}") for item in allowed):
            raise ValueError("Result URL host is not in the allowlist")


def _retryable_status(status_code: int) -> bool:
    return status_code in {429, 502, 503, 504}


async def _retry_delay(attempt: int) -> None:
    await asyncio.sleep(min(0.25 * (2 ** attempt), 1.0) * random.uniform(0.9, 1.1))
