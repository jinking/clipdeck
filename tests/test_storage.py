import pytest

from clipdeck.acquisition.storage import LocalBlobStore


@pytest.mark.asyncio
async def test_blob_store_is_content_addressed(tmp_path) -> None:
    store = LocalBlobStore(tmp_path)
    first = await store.put(b"same bytes", mime_type="text/plain")
    second = await store.put(b"same bytes", mime_type="text/plain")

    assert first.blob_id == second.blob_id
    assert first.storage_uri == second.storage_uri
    assert await store.get(first.blob_id) == b"same bytes"
