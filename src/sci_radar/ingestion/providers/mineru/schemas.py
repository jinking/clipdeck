from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class RemoteState(StrEnum):
    UPLOADING = "uploading"
    SUBMITTED = "submitted"
    POLLING = "polling"
    DONE = "done"
    FAILED = "failed"


def map_remote_state(state: str) -> RemoteState:
    mapping = {
        "waiting-file": RemoteState.UPLOADING,
        "pending": RemoteState.SUBMITTED,
        "running": RemoteState.POLLING,
        "converting": RemoteState.POLLING,
        "done": RemoteState.DONE,
        "failed": RemoteState.FAILED,
    }
    try:
        return mapping[state]
    except KeyError as exc:
        raise ValueError(f"unsupported or unknown MinerU state: {state}") from exc


class FileBatch(BaseModel):
    batch_id: str
    file_urls: list[str] = Field(default_factory=list)


class BatchResult(BaseModel):
    batch_id: str
    state: str
    full_zip_url: str | None = None
    error_code: str | int | None = None
    error_message: str | None = None
