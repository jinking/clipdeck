from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

from clipdeck.acquisition.domain import ResourceType, utcnow
from clipdeck.ingestion.domain.models import (
    EvidenceDocument,
    ExternalParseJob,
    IngestionRun,
    IngestionStatus,
)

T = TypeVar("T")
_SENSITIVE_KEYS = {
    "authorization",
    "token",
    "api_token",
    "upload_url",
    "file_url",
    "full_zip_url",
    "result_url",
    "callback_seed",
    "seed",
}


def _safe_options(options: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in options.items():
        normalized = key.lower()
        if normalized in _SENSITIVE_KEYS or normalized.endswith("_token") or normalized.endswith("_url"):
            continue
        if isinstance(value, dict):
            safe[key] = _safe_options(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, list):
            safe[key] = [item for item in value if isinstance(item, (str, int, float, bool)) or item is None]
    return safe


class SQLiteIngestionRepository:
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
            CREATE TABLE IF NOT EXISTS ingestion_runs (
                id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, resource_type TEXT NOT NULL,
                pipeline_fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0, claimed_by TEXT,
                updated_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                UNIQUE(asset_id, pipeline_fingerprint)
            );
            CREATE INDEX IF NOT EXISTS ix_ingestion_claim
                ON ingestion_runs(status, updated_at);
            CREATE TABLE IF NOT EXISTS external_parse_jobs (
                run_id TEXT PRIMARY KEY, provider TEXT NOT NULL, api_version TEXT NOT NULL,
                batch_id TEXT NOT NULL, remote_state TEXT NOT NULL,
                updated_at TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_external_resume
                ON external_parse_jobs(remote_state, updated_at);
            CREATE TABLE IF NOT EXISTS evidence_documents (
                evidence_id TEXT PRIMARY KEY, run_id TEXT, asset_id TEXT NOT NULL,
                pipeline_fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                markdown_blob_id TEXT, yaml_blob_id TEXT, source_archive_blob_id TEXT,
                created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                UNIQUE(asset_id, pipeline_fingerprint)
            );
            CREATE TABLE IF NOT EXISTS derived_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL,
                role TEXT NOT NULL, blob_id TEXT, sha256 TEXT NOT NULL,
                logical_path TEXT NOT NULL, payload_json TEXT NOT NULL,
                UNIQUE(evidence_id, role, logical_path)
            );
            CREATE TABLE IF NOT EXISTS ingestion_outbox_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, published_at TEXT
            );
            """
        )
        self._connection.commit()

    async def close(self) -> None:
        if self._connection is not None:
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

    async def create_run(
        self,
        *,
        asset_id: UUID | str,
        resource_type: ResourceType,
        pipeline_fingerprint: str,
        status: str | IngestionStatus = IngestionStatus.PENDING,
        converter_name: str | None = None,
        execution_options: dict[str, Any] | None = None,
    ) -> IngestionRun:
        run = IngestionRun(
            asset_id=UUID(str(asset_id)),
            resource_type=resource_type,
            pipeline_fingerprint=pipeline_fingerprint,
            converter_name=converter_name or resource_type.value,
            execution_options=_safe_options(execution_options or {}),
            status=IngestionStatus(status),
        )
        now = utcnow().isoformat()

        def create(db: sqlite3.Connection) -> IngestionRun:
            db.execute(
                """INSERT INTO ingestion_runs(
                       id,asset_id,resource_type,pipeline_fingerprint,status,attempt_count,
                       claimed_by,updated_at,payload_json
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(asset_id,pipeline_fingerprint) DO NOTHING""",
                (
                    str(run.run_id), str(run.asset_id), run.resource_type, run.pipeline_fingerprint,
                    run.status, run.attempt_count, None, now, run.model_dump_json(exclude_none=False),
                ),
            )
            row = db.execute(
                "SELECT payload_json FROM ingestion_runs WHERE asset_id=? AND pipeline_fingerprint=?",
                (str(asset_id), pipeline_fingerprint),
            ).fetchone()
            return IngestionRun.model_validate_json(row["payload_json"])

        return await self._run(create)

    async def save_run(self, run: IngestionRun, *, claimed_by: str | None = None) -> None:
        now = utcnow().isoformat()
        await self._run(lambda db: db.execute(
            """INSERT INTO ingestion_runs(
                   id,asset_id,resource_type,pipeline_fingerprint,status,attempt_count,
                   claimed_by,updated_at,payload_json
               ) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                   attempt_count=excluded.attempt_count, claimed_by=excluded.claimed_by,
                   updated_at=excluded.updated_at, payload_json=excluded.payload_json""",
            (
                str(run.run_id), str(run.asset_id), run.resource_type, run.pipeline_fingerprint,
                run.status, run.attempt_count, claimed_by, now,
                run.model_dump_json(exclude_none=False),
            ),
        ))

    async def get_run(self, run_id: UUID | str) -> IngestionRun | None:
        row = await self._run(lambda db: db.execute(
            "SELECT payload_json FROM ingestion_runs WHERE id=?", (str(run_id),)
        ).fetchone())
        return IngestionRun.model_validate_json(row["payload_json"]) if row else None

    async def get_run_for_asset(
        self, asset_id: UUID | str, pipeline_fingerprint: str
    ) -> IngestionRun | None:
        row = await self._run(lambda db: db.execute(
            "SELECT payload_json FROM ingestion_runs WHERE asset_id=? AND pipeline_fingerprint=?",
            (str(asset_id), pipeline_fingerprint),
        ).fetchone())
        return IngestionRun.model_validate_json(row["payload_json"]) if row else None

    async def list_runs(self, limit: int = 100) -> list[IngestionRun]:
        rows = await self._run(lambda db: db.execute(
            "SELECT payload_json FROM ingestion_runs ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall())
        return [IngestionRun.model_validate_json(row["payload_json"]) for row in rows]

    async def list_recoverable_runs(self) -> list[IngestionRun]:
        terminal = (
            IngestionStatus.SUCCESS,
            IngestionStatus.PARTIAL,
            IngestionStatus.FAILED,
            IngestionStatus.QUARANTINED,
        )
        rows = await self._run(lambda db: db.execute(
            "SELECT payload_json FROM ingestion_runs WHERE status NOT IN (?,?,?,?) ORDER BY updated_at",
            terminal,
        ).fetchall())
        return [IngestionRun.model_validate_json(row["payload_json"]) for row in rows]

    async def claim_next_run(self, *, worker_id: str) -> IngestionRun | None:
        def claim(db: sqlite3.Connection) -> IngestionRun | None:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id,payload_json FROM ingestion_runs WHERE status=? ORDER BY updated_at,id LIMIT 1",
                (IngestionStatus.PENDING,),
            ).fetchone()
            if not row:
                return None
            run = IngestionRun.model_validate_json(row["payload_json"])
            run.status = IngestionStatus.RUNNING
            run.attempt_count += 1
            if run.started_at is None:
                run.started_at = utcnow()
            now = utcnow().isoformat()
            db.execute(
                "UPDATE ingestion_runs SET status=?,attempt_count=?,claimed_by=?,updated_at=?,payload_json=? WHERE id=? AND status=?",
                (run.status, run.attempt_count, worker_id, now, run.model_dump_json(), row["id"], IngestionStatus.PENDING),
            )
            return run
        return await self._run(claim)

    async def mark_stale_running(
        self, *, timeout_seconds: int, recovered_status: str = "pending"
    ) -> int:
        cutoff = (utcnow() - timedelta(seconds=timeout_seconds)).isoformat()
        status = IngestionStatus(recovered_status)

        def recover(db: sqlite3.Connection) -> int:
            rows = db.execute(
                "SELECT id,payload_json FROM ingestion_runs WHERE status=? AND updated_at<=?",
                (IngestionStatus.RUNNING, cutoff),
            ).fetchall()
            for row in rows:
                run = IngestionRun.model_validate_json(row["payload_json"])
                run.status = status
                db.execute(
                    "UPDATE ingestion_runs SET status=?,claimed_by=NULL,updated_at=?,payload_json=? WHERE id=?",
                    (status, utcnow().isoformat(), run.model_dump_json(), row["id"]),
                )
            return len(rows)
        return await self._run(recover)

    async def upsert_external_job(
        self,
        *,
        run_id: str,
        provider: str,
        api_version: str,
        batch_id: str,
        data_id: str,
        remote_state: str,
        request_options: dict[str, Any],
        poll_count: int = 0,
        result_archive_blob_id: str | None = None,
        provider_error: str | None = None,
    ) -> ExternalParseJob:
        existing = await self.get_external_job(run_id)
        job = ExternalParseJob(
            run_id=run_id,
            provider=provider,
            api_version=api_version,
            batch_id=batch_id,
            data_id=data_id,
            remote_state=remote_state,
            request_options=_safe_options(request_options),
            poll_count=poll_count if existing is None else max(poll_count, existing.poll_count),
            result_archive_blob_id=result_archive_blob_id or (existing.result_archive_blob_id if existing else None),
            provider_error=provider_error,
            created_at=existing.created_at if existing else utcnow(),
            updated_at=utcnow(),
        )
        await self._run(lambda db: db.execute(
            """INSERT INTO external_parse_jobs(run_id,provider,api_version,batch_id,remote_state,updated_at,payload_json)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET
               provider=excluded.provider,api_version=excluded.api_version,batch_id=excluded.batch_id,
               remote_state=excluded.remote_state,updated_at=excluded.updated_at,payload_json=excluded.payload_json""",
            (run_id, provider, api_version, batch_id, remote_state, job.updated_at.isoformat(), job.model_dump_json()),
        ))
        return job

    async def get_external_job(self, run_id: str) -> ExternalParseJob | None:
        row = await self._run(lambda db: db.execute(
            "SELECT payload_json FROM external_parse_jobs WHERE run_id=?", (run_id,)
        ).fetchone())
        return ExternalParseJob.model_validate_json(row["payload_json"]) if row else None

    async def list_resumable_jobs(self) -> list[ExternalParseJob]:
        rows = await self._run(lambda db: db.execute(
            "SELECT payload_json FROM external_parse_jobs WHERE remote_state NOT IN ('done','failed') ORDER BY updated_at"
        ).fetchall())
        return [ExternalParseJob.model_validate_json(row["payload_json"]) for row in rows]

    async def save_evidence(self, evidence: EvidenceDocument) -> None:
        def save(db: sqlite3.Connection) -> None:
            db.execute(
                """INSERT INTO evidence_documents(
                       evidence_id,run_id,asset_id,pipeline_fingerprint,status,markdown_blob_id,
                       yaml_blob_id,source_archive_blob_id,created_at,payload_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(evidence_id) DO UPDATE SET status=excluded.status,payload_json=excluded.payload_json""",
                (
                    evidence.evidence_id, evidence.run_id, str(evidence.asset_id), evidence.pipeline_fingerprint,
                    evidence.status, evidence.markdown_blob_id, evidence.yaml_blob_id,
                    evidence.source_archive_blob_id, evidence.created_at.isoformat(), evidence.model_dump_json(),
                ),
            )
            for artifact in evidence.artifacts:
                db.execute(
                    """INSERT OR REPLACE INTO derived_artifacts(
                           evidence_id,role,blob_id,sha256,logical_path,payload_json
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        evidence.evidence_id, artifact.role, artifact.blob_id, artifact.sha256,
                        artifact.logical_path, artifact.model_dump_json(),
                    ),
                )
            db.execute(
                "INSERT INTO ingestion_outbox_events(event_type,aggregate_id,payload_json) VALUES(?,?,?)",
                ("evidence_document.created", evidence.evidence_id, json.dumps({"evidence_id": evidence.evidence_id})),
            )
        await self._run(save)

    async def complete_run_with_evidence(
        self, run: IngestionRun, evidence: EvidenceDocument
    ) -> None:
        run.status = evidence.status
        run.evidence_id = evidence.evidence_id
        run.finished_at = utcnow()

        def complete(db: sqlite3.Connection) -> None:
            db.execute(
                """INSERT INTO evidence_documents(
                       evidence_id,run_id,asset_id,pipeline_fingerprint,status,markdown_blob_id,
                       yaml_blob_id,source_archive_blob_id,created_at,payload_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(evidence_id) DO UPDATE SET status=excluded.status,payload_json=excluded.payload_json""",
                (
                    evidence.evidence_id, evidence.run_id, str(evidence.asset_id), evidence.pipeline_fingerprint,
                    evidence.status, evidence.markdown_blob_id, evidence.yaml_blob_id,
                    evidence.source_archive_blob_id, evidence.created_at.isoformat(), evidence.model_dump_json(),
                ),
            )
            for artifact in evidence.artifacts:
                db.execute(
                    """INSERT OR REPLACE INTO derived_artifacts(
                           evidence_id,role,blob_id,sha256,logical_path,payload_json
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        evidence.evidence_id, artifact.role, artifact.blob_id, artifact.sha256,
                        artifact.logical_path, artifact.model_dump_json(),
                    ),
                )
            db.execute(
                "INSERT INTO ingestion_outbox_events(event_type,aggregate_id,payload_json) VALUES(?,?,?)",
                ("evidence_document.created", evidence.evidence_id, json.dumps({"evidence_id": evidence.evidence_id})),
            )
            db.execute(
                """UPDATE ingestion_runs SET status=?,attempt_count=?,claimed_by=NULL,
                   updated_at=?,payload_json=? WHERE id=?""",
                (
                    run.status, run.attempt_count, utcnow().isoformat(),
                    run.model_dump_json(exclude_none=False), str(run.run_id),
                ),
            )
        await self._run(complete)

    async def get_evidence(self, evidence_id: str) -> EvidenceDocument | None:
        row = await self._run(lambda db: db.execute(
            "SELECT payload_json FROM evidence_documents WHERE evidence_id=?", (evidence_id,)
        ).fetchone())
        return EvidenceDocument.model_validate_json(row["payload_json"]) if row else None

    async def get_evidence_for_asset(
        self, asset_id: UUID | str, pipeline_fingerprint: str
    ) -> EvidenceDocument | None:
        row = await self._run(lambda db: db.execute(
            "SELECT payload_json FROM evidence_documents WHERE asset_id=? AND pipeline_fingerprint=?",
            (str(asset_id), pipeline_fingerprint),
        ).fetchone())
        return EvidenceDocument.model_validate_json(row["payload_json"]) if row else None

    async def list_evidence(self, limit: int = 100) -> list[EvidenceDocument]:
        rows = await self._run(lambda db: db.execute(
            "SELECT payload_json FROM evidence_documents ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall())
        return [EvidenceDocument.model_validate_json(row["payload_json"]) for row in rows]
