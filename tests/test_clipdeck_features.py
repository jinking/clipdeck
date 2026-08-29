"""Tests for the Clipdeck generic-archiver capabilities (Phase 1 + Phase 2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from clipdeck.acquisition import api as acquisition_api
from clipdeck.acquisition.encoding import decode_html, detect_encoding
from clipdeck.acquisition.main import create_app
from clipdeck.ingestion import site_profiles


# ---------------------------------------------------------------------------
# Encoding detection
# ---------------------------------------------------------------------------

def test_decode_html_handles_gbk_meta_charset() -> None:
    html = "<html><head><meta charset='gbk'></head><body><p>网页归档测试内容</p></body></html>"
    data = html.encode("gbk")
    assert detect_encoding(data) and detect_encoding(data).lower().replace("-", "") in {"gbk", "gb2312", "gb18030"}
    assert "网页归档测试内容" in decode_html(data)


def test_decode_html_prefers_content_type_header() -> None:
    data = "<html><body>归档</body></html>".encode("gbk")
    assert decode_html(data, "text/html; charset=gbk") == "<html><body>归档</body></html>"


def test_decode_html_falls_back_to_utf8() -> None:
    assert "普通内容" in decode_html("<p>普通内容</p>".encode("utf-8"))


# ---------------------------------------------------------------------------
# Site profiles (config-driven, no code edits for new sites)
# ---------------------------------------------------------------------------

def test_platform_name_uses_builtin_defaults() -> None:
    site_profiles.load_profiles.cache_clear()
    assert site_profiles.platform_name("mp.weixin.qq.com") == "微信公众号"
    assert site_profiles.platform_name("unknown.example.com") is None


def test_site_profiles_config_overrides_and_disables(tmp_path, monkeypatch) -> None:
    config = tmp_path / "profiles.json"
    config.write_text(json.dumps({
        "platform_domains": {"blog.example.com": "示例博客"},
        "identifier_extractors": {"NCT": False, "ChiCTR": False},
    }), encoding="utf-8")
    monkeypatch.setenv("CLIPDECK_SITE_PROFILES", str(config))
    site_profiles.load_profiles.cache_clear()
    try:
        assert site_profiles.platform_name("blog.example.com") == "示例博客"
        assert "NCT" not in site_profiles.enabled_identifier_types()
        from clipdeck.ingestion.metadata import extract_identifiers
        found = extract_identifiers("Trial NCT01234567 registered.")
        assert all(item["type"] != "NCT" for item in found)
    finally:
        monkeypatch.delenv("CLIPDECK_SITE_PROFILES")
        site_profiles.load_profiles.cache_clear()


# ---------------------------------------------------------------------------
# Full-text search over the evidence filesystem
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_text_search_finds_archived_content(tmp_path: Path) -> None:
    app = create_app(data_root=tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                "/api/v1/acquisitions/text",
                json={"text": "# 量子计算综述\n\n这篇笔记讨论了纠错码与量子霸权实验。", "display_name": "笔记"},
            )
            results = (await client.get("/api/v1/search?q=量子霸权")).json()["results"]
            assert len(results) == 1
            assert results[0]["title"] == "量子计算综述"
            assert "纠错码" in results[0]["snippet"]

            empty = (await client.get("/api/v1/search?q=不存在的词汇")).json()["results"]
            assert empty == []

            bad = await client.get("/api/v1/search")
            assert bad.status_code == 422


@pytest.mark.asyncio
async def test_evidence_list_exposes_title(tmp_path: Path) -> None:
    app = create_app(data_root=tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                "/api/v1/acquisitions/text",
                json={"text": "# 可检索的标题\n\n内容", "display_name": "note"},
            )
            evidence = (await client.get("/api/v1/evidence")).json()[0]
            assert evidence["title"] == "可检索的标题"
            assert evidence["tags"] == []


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tag_roundtrip_and_filtering(tmp_path: Path) -> None:
    app = create_app(data_root=tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/api/v1/acquisitions/text", json={"text": "# 归档学\n\n正文"})
            evidence_id = (await client.get("/api/v1/evidence")).json()[0]["evidence_id"]

            updated = await client.put(f"/api/v1/evidence/{evidence_id}/tags", json={"tags": ["量子", " 量子 ", "阅读"]})
            assert updated.status_code == 200
            assert updated.json()["tags"] == ["量子", "阅读"]

            tags = (await client.get("/api/v1/tags")).json()
            assert {item["name"] for item in tags} == {"量子", "阅读"}
            assert all(item["count"] == 1 for item in tags)

            listed = (await client.get("/api/v1/evidence")).json()[0]
            assert listed["tags"] == ["量子", "阅读"]

            by_tag = (await client.get("/api/v1/search?tag=量子")).json()["results"]
            assert [item["evidence_id"] for item in by_tag] == [evidence_id]

            # Replacing with an empty list clears the tags.
            await client.put(f"/api/v1/evidence/{evidence_id}/tags", json={"tags": []})
            assert (await client.get("/api/v1/evidence")).json()[0]["tags"] == []


# ---------------------------------------------------------------------------
# Batch import + bookmarklet quick-save
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_submission_dedupes_and_rejects(tmp_path: Path, monkeypatch) -> None:
    async def no_execute(request, task_id):  # keep the test offline
        return None
    monkeypatch.setattr(acquisition_api, "execute_task", no_execute)

    app = create_app(data_root=tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/acquisitions/batch", json={"urls": [
                "https://example.com/a",
                "https://example.com/a",       # exact duplicate within the batch
                "https://example.com/b",
                "javascript:alert(1)",          # rejected by the classifier
            ]})
            assert response.status_code == 201
            data = response.json()
            assert len(data["submitted"]) == 2
            reasons = [item["reason"] for item in data["rejected"]]
            assert "duplicate_in_batch" in reasons
            assert any("HTTP(S)" in reason for reason in reasons)


@pytest.mark.asyncio
async def test_quick_save_redirects_back_to_ui(tmp_path: Path, monkeypatch) -> None:
    async def no_execute(request, task_id):
        return None
    monkeypatch.setattr(acquisition_api, "execute_task", no_execute)

    app = create_app(data_root=tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False,
        ) as client:
            response = await client.get("/api/v1/save", params={"url": "https://example.com/post"})
            assert response.status_code == 303
            assert response.headers["location"] == "/?saved=1"

            bad = await client.get("/api/v1/save", params={"url": "ftp://example.com/x"})
            assert bad.status_code == 422


# ---------------------------------------------------------------------------
# Environment compatibility after the rename
# ---------------------------------------------------------------------------

def test_legacy_env_var_still_selects_data_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SCI_ACQUISITION_DATA", str(tmp_path / "legacy-data"))
    monkeypatch.delenv("CLIPDECK_DATA", raising=False)
    from clipdeck.acquisition.main import _env
    assert _env("CLIPDECK_DATA", legacy="SCI_ACQUISITION_DATA") == str(tmp_path / "legacy-data")


def test_new_env_var_wins_over_legacy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SCI_ACQUISITION_DATA", str(tmp_path / "old"))
    monkeypatch.setenv("CLIPDECK_DATA", str(tmp_path / "new"))
    from clipdeck.acquisition.main import _env
    assert _env("CLIPDECK_DATA", legacy="SCI_ACQUISITION_DATA") == str(tmp_path / "new")


def test_decode_html_strips_utf8_bom() -> None:
    bom_html = b"\xef\xbb\xbf<html><body><p>\xe6\xb5\x8b\xe8\xaf\x95\xe6\x96\x87\xe6\x9c\xac</p></body></html>"
    decoded = decode_html(bom_html)
    assert not decoded.startswith("\ufeff")
    assert "测试文本" in decoded


def test_custom_llm_prompt_with_json_curlies_does_not_crash(tmp_path: Path, monkeypatch) -> None:
    prompt_file = tmp_path / "custom_prompt.txt"
    prompt_file.write_text("提取如下内容并输出 JSON 格式 {\"title\": \"xxx\"}：\n{content}", encoding="utf-8")

    config = tmp_path / "profiles.json"
    config.write_text(json.dumps({"llm_extract_prompt_path": str(prompt_file)}), encoding="utf-8")

    monkeypatch.setenv("CLIPDECK_SITE_PROFILES", str(config))
    site_profiles.reload_profiles()
    try:
        from clipdeck.ingestion.llm.extractor import LLMArticleExtractor
        extractor = LLMArticleExtractor(api_key="test-key")
        built = extractor._build_prompt("这是文章正文")
        assert "这是文章正文" in built
        assert "{\"title\": \"xxx\"}" in built
    finally:
        monkeypatch.delenv("CLIPDECK_SITE_PROFILES")
        site_profiles.reload_profiles()


@pytest.mark.asyncio
async def test_acquisition_service_concurrency_semaphore(tmp_path: Path) -> None:
    from clipdeck.acquisition.domain import AcquisitionInput, SourceKind
    from clipdeck.acquisition.repository import SQLiteRepository
    from clipdeck.acquisition.service import AcquisitionService
    from clipdeck.acquisition.storage import LocalBlobStore

    repo = SQLiteRepository(tmp_path / "acq.db")
    await repo.initialize()
    blob = LocalBlobStore(tmp_path / "blobs")
    service = AcquisitionService(repository=repo, blob_store=blob, max_concurrency=2)
    assert service._semaphore._value == 2

    task1 = await service.submit(AcquisitionInput(source_kind=SourceKind.TEXT, text="hello 1"))
    task2 = await service.submit(AcquisitionInput(source_kind=SourceKind.TEXT, text="hello 2"))
    asset1 = await service.execute(task1.task_id)
    asset2 = await service.execute(task2.task_id)
    assert asset1 is not None
    assert asset2 is not None
    await repo.close()

