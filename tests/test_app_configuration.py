from __future__ import annotations

import pytest

from clipdeck.acquisition.main import create_app


@pytest.mark.asyncio
async def test_openai_key_alone_never_enables_external_llm(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leave-process")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.delenv("CLIPDECK_LLM_EXTERNAL_PROCESSING_ALLOWED", raising=False)

    app = create_app(data_root=tmp_path)
    async with app.router.lifespan_context(app):
        assert app.state.ingestion_service.llm_extractor is None


@pytest.mark.asyncio
async def test_explicit_llm_key_without_opt_in_stays_local(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LLM_API_KEY", "explicit-but-disabled")
    monkeypatch.setenv("CLIPDECK_LLM_EXTERNAL_PROCESSING_ALLOWED", "false")

    app = create_app(data_root=tmp_path)
    async with app.router.lifespan_context(app):
        assert app.state.ingestion_service.llm_extractor is None


@pytest.mark.asyncio
async def test_llm_requires_explicit_key_and_external_processing_opt_in(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LLM_API_KEY", "explicit-llm-key")
    monkeypatch.setenv("CLIPDECK_LLM_EXTERNAL_PROCESSING_ALLOWED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")

    app = create_app(data_root=tmp_path)
    async with app.router.lifespan_context(app):
        extractor = app.state.ingestion_service.llm_extractor
        assert extractor is not None
        assert extractor.api_key == "explicit-llm-key"
        assert extractor.base_url == "https://llm.example/v1"
        assert extractor.model == "MiniMax-M3"
        assert extractor.thinking_mode == "disabled"


def test_llm_base_url_rejects_insecure_or_credentialed_origins(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LLM_API_KEY", "explicit-llm-key")
    monkeypatch.setenv("CLIPDECK_LLM_EXTERNAL_PROCESSING_ALLOWED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://user:pass@llm.example/v1")

    with pytest.raises(ValueError, match="HTTPS|credentials"):
        create_app(data_root=tmp_path)


@pytest.mark.asyncio
async def test_mineru_environment_builds_real_client_with_all_settings(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MINERU_BASE_URL", "https://mineru.example")
    monkeypatch.setenv("MINERU_RESULT_HOSTS", "downloads.example, CDN.EXAMPLE ")
    monkeypatch.setenv("MINERU_TIMEOUT_SECONDS", "17")
    monkeypatch.setenv("MINERU_MAX_RESULT_ZIP_BYTES", "12345")

    app = create_app(data_root=tmp_path, mineru_token="fixture-token")
    async with app.router.lifespan_context(app):
        client = app.state.ingestion_service.mineru_client
        assert client is not None
        assert client.settings.token.get_secret_value() == "fixture-token"
        assert client.settings.base_url == "https://mineru.example"
        assert client.settings.result_host_allowlist == {"downloads.example", "cdn.example"}
        assert client.settings.timeout_seconds == 17
        assert client.settings.max_result_zip_bytes == 12345
