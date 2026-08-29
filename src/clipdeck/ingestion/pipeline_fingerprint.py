from __future__ import annotations

import hashlib
import json

from clipdeck.ingestion.domain.models import IngestionOptions


def pipeline_fingerprint(
    options: IngestionOptions,
    *,
    pipeline_version: str = "1.1.0",
    variant: dict[str, str | None] | None = None,
) -> str:
    payload = options.model_dump_json(exclude={"external_processing_allowed", "sensitive_source"})
    if variant is not None:
        payload = f"{payload}:{json.dumps(variant, sort_keys=True, separators=(',', ':'))}"
    return hashlib.sha256(f"{pipeline_version}:{payload}".encode("utf-8")).hexdigest()
