"""Shared classification for filesystem search targets that may hold secrets.

The Action Server applies the same policy again before it walks the remote
filesystem.  This helper is intentionally about *explicit* targets: a broad
directory search is kept safe by the server-side default exclusions, while a
call that names one of these paths may opt into sensitive results only after
the permission layer has approved it.
"""

from pathlib import PurePosixPath


def is_sensitive_path(value: object) -> bool:
    """Return whether a path or glob explicitly names a guarded secret path."""
    raw = str(value or "").replace("\\", "/")
    parts = PurePosixPath(raw).parts
    for part in parts:
        lowered = part.casefold()
        if lowered.startswith(".env"):
            return True
        if lowered == ".ssh" or "credentials" in lowered:
            return True
    return False


def explicitly_requests_sensitive(path: object, pattern: object = "") -> bool:
    """Return whether a search root or its selector explicitly names secrets."""
    return is_sensitive_path(path) or is_sensitive_path(pattern)


def casefold_sensitive_subject(value: object) -> str | None:
    """Return a canonical subject only when it contains a guarded marker.

    Permission glob matching remains case-sensitive for user-authored exact
    rules. Callers retain the original subject and append this projection, so
    platform defaults cannot be bypassed by spelling `.ENV` or `.SSH` while
    existing precise rules keep their original semantics.
    """
    raw = str(value or "")
    folded = raw.casefold()
    if any(marker in folded for marker in (".env", ".ssh", "credentials")):
        return folded
    return None
