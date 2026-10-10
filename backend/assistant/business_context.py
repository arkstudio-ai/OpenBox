"""Credential redaction for assistant tool observations.

V2 (docs/PERSONAL_ASSISTANT_DESIGN_V2.md 4.2) uses each observation as it was
read. Nothing is captured for later replay or compared at a checkpoint.
"""
from memory.redaction import redact_credentials


def _safe(value):
    if isinstance(value, str):
        return redact_credentials(value)
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    return value
