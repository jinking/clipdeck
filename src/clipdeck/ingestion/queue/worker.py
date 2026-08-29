from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from clipdeck.ingestion.application import IngestionService
from clipdeck.ingestion.domain.models import EvidenceDocument, IngestionOptions
from clipdeck.ingestion.repository import SQLiteIngestionRepository

logger = logging.getLogger(__name__)


class WorkerClosedError(RuntimeError):
    """Raised for submitted work that cannot finish because the worker closed."""


@dataclass
class _WorkItem:
    asset_id: UUID
    fingerprint: str | None
    options: IngestionOptions
    future: asyncio.Future[EvidenceDocument] | None = None


class SingleIngestionWorker:
    """One in-process consumer backed by durable SQLite run/job state."""

    def __init__(
        self,
        service: IngestionService,
        repository: SQLiteIngestionRepository,
        *,
        close_timeout_seconds: float = 5,
    ):
        self.service = service
        self.repository = repository
        self.queue: asyncio.Queue[_WorkItem | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self.close_timeout_seconds = close_timeout_seconds
        self._closed = False

    async def start(self) -> None:
        self._closed = False
        await self.repository.mark_stale_running(timeout_seconds=0, recovered_status="pending")
        self._task = asyncio.create_task(self._run(), name="clipdeck-ingestion-worker")
        for run in await self.repository.list_recoverable_runs():
            job = await self.repository.get_external_job(str(run.run_id))
            if run.execution_options:
                options = IngestionOptions.model_validate(run.execution_options)
            elif job:
                # Compatibility for jobs created before execution options were
                # persisted on the Run.  An existing external job proves that
                # the explicit egress gate was passed previously.
                options = IngestionOptions.model_validate(job.request_options)
                options.external_processing_allowed = True
            else:
                options = IngestionOptions()
            validator = getattr(self.service, "validate_recoverable_run", None)
            if validator is not None and not await validator(run, options):
                continue
            await self.enqueue(run.asset_id, fingerprint=run.pipeline_fingerprint, options=options)

    async def close(self) -> None:
        self._closed = True
        if self._task is None:
            self._fail_queued()
            return
        if self._task.done():
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
            self._fail_queued()
            return
        await self.queue.put(None)
        try:
            await asyncio.wait_for(self._task, timeout=self.close_timeout_seconds)
        except TimeoutError:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self._fail_queued()

    def _fail_queued(self) -> None:
        while True:
            try:
                item = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if item is not None and item.future and not item.future.done():
                    item.future.set_exception(WorkerClosedError("Ingestion worker closed before completing work"))
            finally:
                self.queue.task_done()

    async def enqueue(
        self,
        asset_id: UUID,
        *,
        fingerprint: str | None = None,
        options: IngestionOptions | None = None,
    ) -> None:
        if self._closed:
            raise WorkerClosedError("Ingestion worker is closed")
        await self.queue.put(_WorkItem(asset_id, fingerprint, options or IngestionOptions()))

    async def submit(
        self,
        asset_id: UUID,
        *,
        fingerprint: str | None = None,
        options: IngestionOptions | None = None,
    ) -> EvidenceDocument:
        if self._closed:
            raise WorkerClosedError("Ingestion worker is closed")
        future: asyncio.Future[EvidenceDocument] = asyncio.get_running_loop().create_future()
        await self.queue.put(_WorkItem(asset_id, fingerprint, options or IngestionOptions(), future))
        return await future

    async def _run(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                if item is None:
                    return
                try:
                    result = await self.service.ingest(
                        item.asset_id,
                        pipeline_fingerprint=item.fingerprint,
                        options=item.options,
                    )
                    if item.future and not item.future.done():
                        item.future.set_result(result)
                except asyncio.CancelledError:
                    if item.future and not item.future.done():
                        item.future.set_exception(
                            WorkerClosedError("Ingestion worker closed while processing work")
                        )
                    raise
                except Exception as exc:
                    logger.error(
                        "ingestion failed asset_id=%s error_type=%s",
                        item.asset_id,
                        type(exc).__name__,
                    )
                    if item.future and not item.future.done():
                        item.future.set_exception(exc)
            finally:
                self.queue.task_done()
