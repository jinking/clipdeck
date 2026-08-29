class ExternalProcessingDenied(PermissionError):
    """The raw document is not authorized for an external parser."""


def ensure_external_processing_allowed(
    external_processing_allowed: bool,
    sensitive_source: bool,
) -> None:
    if sensitive_source:
        raise ExternalProcessingDenied("Sensitive sources cannot be sent to external processing")
    if not external_processing_allowed:
        raise ExternalProcessingDenied("External processing requires explicit permission")
