import pytest

from sci_radar.ingestion.domain.models import IngestionOptions
from sci_radar.ingestion.pipeline_fingerprint import pipeline_fingerprint
from sci_radar.ingestion.policy import ExternalProcessingDenied, ensure_external_processing_allowed
from sci_radar.ingestion.providers.mineru.schemas import RemoteState, map_remote_state


def test_pipeline_fingerprint_is_stable_and_changes_with_provider_options() -> None:
    first = pipeline_fingerprint(IngestionOptions(model_version="vlm", language="ch"))
    reordered = pipeline_fingerprint(IngestionOptions(language="ch", model_version="vlm"))
    pipeline = pipeline_fingerprint(IngestionOptions(model_version="pipeline", language="ch"))

    assert first == reordered
    assert first != pipeline


def test_external_document_processing_requires_explicit_permission() -> None:
    with pytest.raises(ExternalProcessingDenied):
        ensure_external_processing_allowed(False, sensitive_source=False)
    with pytest.raises(ExternalProcessingDenied):
        ensure_external_processing_allowed(True, sensitive_source=True)

    ensure_external_processing_allowed(True, sensitive_source=False)


@pytest.mark.parametrize(
    "remote,expected",
    [
        ("waiting-file", RemoteState.UPLOADING),
        ("pending", RemoteState.SUBMITTED),
        ("running", RemoteState.POLLING),
        ("converting", RemoteState.POLLING),
        ("done", RemoteState.DONE),
        ("failed", RemoteState.FAILED),
    ],
)
def test_mineru_remote_state_mapping(remote: str, expected: RemoteState) -> None:
    assert map_remote_state(remote) is expected


def test_unknown_mineru_state_is_not_silently_treated_as_success() -> None:
    with pytest.raises(ValueError, match="unknown|unsupported"):
        map_remote_state("future-state")
