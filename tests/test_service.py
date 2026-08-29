import pytest

from sci_radar.acquisition.domain import AcquisitionInput, SourceKind, TaskStatus
from sci_radar.acquisition.repository import SQLiteRepository
from sci_radar.acquisition.service import AcquisitionService
from sci_radar.acquisition.storage import LocalBlobStore


@pytest.fixture
async def service(tmp_path):
    repository = SQLiteRepository(tmp_path / "acquisition.db")
    await repository.initialize()
    yield AcquisitionService(repository=repository, blob_store=LocalBlobStore(tmp_path / "data"))
    await repository.close()


@pytest.mark.asyncio
async def test_pasted_text_becomes_raw_asset_without_semantic_parsing(service) -> None:
    task = await service.submit(
        AcquisitionInput(source_kind=SourceKind.TEXT, text="原始研究笔记", display_name="手工粘贴")
    )
    asset = await service.execute(task.task_id)

    assert asset is not None
    assert asset.resource_type.value == "text"
    assert asset.primary_blob.role.value == "pasted_text"
    assert asset.version_no == 1
    assert asset.acquisition_status is TaskStatus.SUCCESS
    assert not hasattr(asset, "title")


@pytest.mark.asyncio
async def test_same_text_source_creates_append_only_versions(service) -> None:
    first_task = await service.submit(
        AcquisitionInput(source_kind=SourceKind.TEXT, text="v1", source_key="notes:weekly")
    )
    first = await service.execute(first_task.task_id)
    second_task = await service.submit(
        AcquisitionInput(source_kind=SourceKind.TEXT, text="v2", source_key="notes:weekly", force_refetch=True)
    )
    second = await service.execute(second_task.task_id)

    assert first and second
    assert second.version_no == 2
    assert second.previous_asset_id == first.asset_id
    assert second.changed_from_previous is True


@pytest.mark.asyncio
async def test_uploaded_pdf_is_stored_raw_and_marked_for_ingestion(service) -> None:
    task = await service.submit(
        AcquisitionInput(
            source_kind=SourceKind.FILE,
            filename="paper.pdf",
            mime_type="application/pdf",
            data=b"%PDF-1.7 fake fixture",
        )
    )
    asset = await service.execute(task.task_id)

    assert asset and asset.resource_type.value == "pdf"
    assert asset.provider_meta["ingestion_hint"] == "document_text_extraction"
    assert asset.primary_blob.mime_type == "application/pdf"

    view_path = asset.provider_meta["asset_view_uri"]
    from pathlib import Path
    import os
    view = Path(view_path)
    readable_pdf = view / "original.pdf"
    assert readable_pdf.read_bytes() == b"%PDF-1.7 fake fixture"
    assert (view / "manifest.json").exists()
    assert os.stat(readable_pdf).st_ino == os.stat(asset.primary_blob.storage_uri).st_ino
    assert os.stat(readable_pdf).st_mode & 0o222 == 0


@pytest.mark.asyncio
async def test_recovery_repairs_task_when_asset_was_already_published(service) -> None:
    task = await service.submit(
        AcquisitionInput(source_kind=SourceKind.TEXT, text="crash-gap", source_key="notes:crash-gap")
    )
    original = await service.execute(task.task_id)
    assert original is not None

    # Simulate a database created by an older build that crashed after the
    # RawAsset/outbox commit but before completing the task row.
    persisted_task = await service.repository.get_task(task.task_id)
    assert persisted_task is not None
    persisted_task.status = TaskStatus.RUNNING
    persisted_task.latest_asset_id = None
    await service.repository.save_task(persisted_task)

    recovered = await service.execute(task.task_id)
    assets = await service.repository.list_assets()
    repaired_task = await service.repository.get_task(task.task_id)

    assert recovered is not None
    assert recovered.asset_id == original.asset_id
    assert [asset.asset_id for asset in assets] == [original.asset_id]
    assert repaired_task is not None
    assert repaired_task.latest_asset_id == original.asset_id
    assert repaired_task.status == TaskStatus.SUCCESS
