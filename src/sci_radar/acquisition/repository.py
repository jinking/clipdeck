from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

from sci_radar.acquisition.domain import AcquisitionTask, BlobRef, FetchAttempt, RawAsset, TaskStatus

T = TypeVar("T")


class SQLiteRepository:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS acquisition_tasks (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, priority INTEGER NOT NULL,
                created_at TEXT NOT NULL, resource_type TEXT NOT NULL, source_kind TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_tasks_status_priority ON acquisition_tasks(status, priority DESC, created_at);
            CREATE TABLE IF NOT EXISTS fetch_attempts (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
                status TEXT NOT NULL, started_at TEXT NOT NULL, error_code TEXT,
                payload_json TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES acquisition_tasks(id)
            );
            CREATE INDEX IF NOT EXISTS ix_attempts_task ON fetch_attempts(task_id, attempt_no);
            CREATE TABLE IF NOT EXISTS raw_assets (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                resource_key TEXT NOT NULL, version_no INTEGER NOT NULL,
                resource_type TEXT NOT NULL, fetched_at TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(resource_key, version_no),
                FOREIGN KEY(task_id) REFERENCES acquisition_tasks(id),
                FOREIGN KEY(attempt_id) REFERENCES fetch_attempts(id)
            );
            CREATE INDEX IF NOT EXISTS ix_assets_resource ON raw_assets(resource_key, version_no DESC);
            CREATE TABLE IF NOT EXISTS outbox_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, published_at TEXT
            );
            """
        )
        self._connection.commit()

    async def close(self) -> None:
        if self._connection:
            self._connection.close()
            self._connection = None

    async def _run(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        if self._connection is None:
            raise RuntimeError("Repository is not initialized")
        async with self._lock:
            connection = self._connection
            def execute() -> T:
                try:
                    result = operation(connection)
                    connection.commit()
                    return result
                except Exception:
                    connection.rollback()
                    raise
            return await asyncio.to_thread(execute)

    @staticmethod
    def _dump(model: Any) -> str:
        return model.model_dump_json(exclude_none=False)

    async def save_task(self, task: AcquisitionTask) -> None:
        await self._run(lambda db: db.execute(
            """INSERT INTO acquisition_tasks(id,status,priority,created_at,resource_type,source_kind,payload_json)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status, priority=excluded.priority,
               resource_type=excluded.resource_type, source_kind=excluded.source_kind, payload_json=excluded.payload_json""",
            (str(task.task_id), task.status, task.priority, task.created_at.isoformat(), task.resource_type, task.source_kind, self._dump(task)),
        ))

    async def get_task(self, task_id: UUID | str) -> AcquisitionTask | None:
        row = await self._run(lambda db: db.execute("SELECT payload_json FROM acquisition_tasks WHERE id=?", (str(task_id),)).fetchone())
        return AcquisitionTask.model_validate_json(row["payload_json"]) if row else None

    async def list_tasks(self, limit: int = 50) -> list[AcquisitionTask]:
        rows = await self._run(lambda db: db.execute(
            "SELECT payload_json FROM acquisition_tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall())
        return [AcquisitionTask.model_validate_json(row["payload_json"]) for row in rows]

    async def list_recoverable_tasks(self) -> list[AcquisitionTask]:
        rows = await self._run(lambda db: db.execute(
            "SELECT payload_json FROM acquisition_tasks WHERE status IN (?,?) ORDER BY priority DESC,created_at",
            (TaskStatus.PENDING, TaskStatus.RUNNING),
        ).fetchall())
        return [AcquisitionTask.model_validate_json(row["payload_json"]) for row in rows]

    async def claim_next_task(self) -> AcquisitionTask | None:
        def claim(db: sqlite3.Connection):
            row = db.execute(
                "SELECT payload_json FROM acquisition_tasks WHERE status=? ORDER BY priority DESC, created_at LIMIT 1",
                (TaskStatus.PENDING,),
            ).fetchone()
            return AcquisitionTask.model_validate_json(row["payload_json"]) if row else None
        return await self._run(claim)

    async def save_attempt(self, attempt: FetchAttempt) -> None:
        await self._run(lambda db: db.execute(
            """INSERT INTO fetch_attempts(id,task_id,attempt_no,status,started_at,error_code,payload_json)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,
               error_code=excluded.error_code,payload_json=excluded.payload_json""",
            (str(attempt.attempt_id), str(attempt.task_id), attempt.attempt_no, attempt.status,
             attempt.started_at.isoformat(), attempt.error_code, self._dump(attempt)),
        ))

    async def list_attempts(self, task_id: UUID | str) -> list[FetchAttempt]:
        rows = await self._run(lambda db: db.execute(
            "SELECT payload_json FROM fetch_attempts WHERE task_id=? ORDER BY attempt_no", (str(task_id),)
        ).fetchall())
        return [FetchAttempt.model_validate_json(row["payload_json"]) for row in rows]

    async def save_asset(self, asset: RawAsset) -> None:
        payload = self._dump(asset)
        def save(db: sqlite3.Connection) -> None:
            db.execute(
                """INSERT INTO raw_assets(id,task_id,attempt_id,resource_key,version_no,resource_type,fetched_at,raw_sha256,payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (str(asset.asset_id), str(asset.task_id), str(asset.attempt_id), asset.resource_key,
                 asset.version_no, asset.resource_type, asset.fetched_at.isoformat(), asset.raw_sha256, payload),
            )
            db.execute(
                "INSERT INTO outbox_events(event_type,aggregate_id,payload_json) VALUES(?,?,?)",
                ("raw_asset.created", str(asset.asset_id), json.dumps({"asset_id": str(asset.asset_id), "resource_type": asset.resource_type})),
            )
        await self._run(save)

    async def complete_task_with_asset(self, task: AcquisitionTask, asset: RawAsset) -> None:
        """Publish RawAsset/outbox and complete its task in one SQLite transaction."""
        asset_payload = self._dump(asset)

        def complete(db: sqlite3.Connection) -> None:
            db.execute(
                """INSERT INTO raw_assets(id,task_id,attempt_id,resource_key,version_no,resource_type,fetched_at,raw_sha256,payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (str(asset.asset_id), str(asset.task_id), str(asset.attempt_id), asset.resource_key,
                 asset.version_no, asset.resource_type, asset.fetched_at.isoformat(), asset.raw_sha256, asset_payload),
            )
            db.execute(
                "INSERT INTO outbox_events(event_type,aggregate_id,payload_json) VALUES(?,?,?)",
                ("raw_asset.created", str(asset.asset_id), json.dumps({"asset_id": str(asset.asset_id), "resource_type": asset.resource_type})),
            )
            db.execute(
                """UPDATE acquisition_tasks SET status=?,priority=?,resource_type=?,source_kind=?,payload_json=?
                   WHERE id=?""",
                (task.status, task.priority, task.resource_type, task.source_kind,
                 self._dump(task), str(task.task_id)),
            )

        await self._run(complete)

    async def update_asset_manifest(self, asset: RawAsset) -> None:
        await self._run(lambda db: db.execute(
            "UPDATE raw_assets SET payload_json=? WHERE id=?",
            (self._dump(asset), str(asset.asset_id)),
        ))

    async def get_asset(self, asset_id: UUID | str) -> RawAsset | None:
        row = await self._run(lambda db: db.execute("SELECT payload_json FROM raw_assets WHERE id=?", (str(asset_id),)).fetchone())
        return RawAsset.model_validate_json(row["payload_json"]) if row else None

    async def get_asset_for_task(self, task_id: UUID | str) -> RawAsset | None:
        row = await self._run(lambda db: db.execute(
            "SELECT payload_json FROM raw_assets WHERE task_id=? ORDER BY fetched_at DESC LIMIT 1",
            (str(task_id),),
        ).fetchone())
        return RawAsset.model_validate_json(row["payload_json"]) if row else None

    async def latest_asset(self, resource_key: str) -> RawAsset | None:
        row = await self._run(lambda db: db.execute(
            "SELECT payload_json FROM raw_assets WHERE resource_key=? ORDER BY version_no DESC LIMIT 1", (resource_key,)
        ).fetchone())
        return RawAsset.model_validate_json(row["payload_json"]) if row else None

    async def list_assets(self, limit: int = 50) -> list[RawAsset]:
        rows = await self._run(lambda db: db.execute(
            "SELECT payload_json FROM raw_assets ORDER BY fetched_at DESC LIMIT ?", (limit,)
        ).fetchall())
        return [RawAsset.model_validate_json(row["payload_json"]) for row in rows]

    async def find_blob(self, blob_id: str) -> BlobRef | None:
        rows = await self._run(lambda db: db.execute(
            "SELECT payload_json FROM raw_assets ORDER BY fetched_at DESC"
        ).fetchall())
        for row in rows:
            asset = RawAsset.model_validate_json(row["payload_json"])
            for blob in [asset.primary_blob, *asset.blobs, *asset.child_assets]:
                if blob.blob_id == blob_id:
                    return blob
        return None

    async def summary(self) -> dict[str, Any]:
        def query(db: sqlite3.Connection) -> dict[str, Any]:
            tasks = db.execute("SELECT COUNT(*) count FROM acquisition_tasks").fetchone()["count"]
            assets = db.execute("SELECT COUNT(*) count FROM raw_assets").fetchone()["count"]
            blobs = db.execute("SELECT COALESCE(SUM(json_extract(payload_json, '$.primary_blob.size_bytes')), 0) size FROM raw_assets").fetchone()["size"]
            status_rows = db.execute("SELECT status, COUNT(*) count FROM acquisition_tasks GROUP BY status").fetchall()
            return {"tasks": tasks, "assets": assets, "stored_bytes": blobs, "by_status": {r["status"]: r["count"] for r in status_rows}}
        return await self._run(query)
