"""Bounded validation for durable personal-Skill ZIP snapshots.

The database copy of a personal Skill is an executable supply-chain artifact:
it is downloaded from one sandbox generation and can later be restored into a
different generation.  Treat its central directory as untrusted even when the
bytes came from an OpenBox Action Server.  This module deliberately performs
no extraction; callers use it before persisting, forwarding, or publishing the
archive.

The Action Server mirrors this policy at the execution boundary.  Keep the
public constants stable so the two implementations can be regression-tested
against the same resource envelope.
"""

from __future__ import annotations

import io
import stat
import struct
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath

SKILL_ARCHIVE_POLICY_VERSION = "bounded-zip-v1"
SKILL_ARCHIVE_MAX_COMPRESSED_BYTES = 50 * 1024 * 1024
SKILL_ARCHIVE_MAX_ENTRIES = 1_000
SKILL_ARCHIVE_MAX_FILE_BYTES = 10 * 1024 * 1024
SKILL_ARCHIVE_MAX_TOTAL_BYTES = 50 * 1024 * 1024
SKILL_ARCHIVE_MAX_RATIO = 200
SKILL_ARCHIVE_RATIO_MIN_BYTES = 1024 * 1024
SKILL_ARCHIVE_MAX_PATH_BYTES = 512
SKILL_ARCHIVE_MAX_DEPTH = 32
_READ_CHUNK_BYTES = 64 * 1024
_ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_EOCD_SIGNATURE = b"PK\x05\x06"
_CENTRAL_FILE_SIGNATURE = b"PK\x01\x02"
_EOCD_FIXED_BYTES = 22
_CENTRAL_FILE_FIXED_BYTES = 46


class SkillArchiveValidationError(ValueError):
    """A Skill archive is malformed, ambiguous, or exceeds its budget."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class SkillArchiveSummary:
    entries: int
    files: int
    uncompressed_bytes: int
    compressed_payload_bytes: int


def _reject(code: str, message: str) -> None:
    raise SkillArchiveValidationError(code, message)


def _preflight_central_directory(archive: bytes) -> None:
    """Count raw central records before ``zipfile`` allocates ZipInfo objects."""
    search_start = max(0, len(archive) - (_EOCD_FIXED_BYTES + 0xFFFF))
    search_end = len(archive)
    eocd_offset = -1
    while search_end > search_start:
        candidate = archive.rfind(_EOCD_SIGNATURE, search_start, search_end)
        if candidate < 0:
            break
        if candidate + _EOCD_FIXED_BYTES <= len(archive):
            comment_size = struct.unpack_from("<H", archive, candidate + 20)[0]
            if candidate + _EOCD_FIXED_BYTES + comment_size == len(archive):
                eocd_offset = candidate
                break
        search_end = candidate
    if eocd_offset < 0:
        _reject("invalid_zip", "Skill ZIP has no valid end-of-directory record")

    (
        _signature,
        disk_number,
        central_disk,
        entries_on_disk,
        total_entries,
        central_size,
        central_offset,
        _comment_size,
    ) = struct.unpack_from("<4s4H2LH", archive, eocd_offset)
    if disk_number != 0 or central_disk != 0 or entries_on_disk != total_entries:
        _reject("multi_disk", "Multi-disk Skill ZIPs are not supported")
    # ZIP64 is unnecessary under this policy's 50 MiB / 1000-entry envelope.
    # Rejecting its sentinel also avoids trusting a second, attacker-steerable
    # directory structure before the cheap bounded scan is complete.
    if (
        total_entries == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    ):
        _reject("zip64", "ZIP64 Skill archives are not supported")
    if total_entries > SKILL_ARCHIVE_MAX_ENTRIES:
        _reject("too_many_entries", "Skill ZIP contains too many entries")

    concatenated_prefix = eocd_offset - central_size - central_offset
    central_start = central_offset + concatenated_prefix
    central_end = central_start + central_size
    if central_start < 0 or central_end != eocd_offset:
        _reject("invalid_zip", "Skill ZIP central directory has invalid bounds")

    count = 0
    cursor = central_start
    while cursor < central_end:
        if (
            cursor + _CENTRAL_FILE_FIXED_BYTES > central_end
            or archive[cursor : cursor + 4] != _CENTRAL_FILE_SIGNATURE
        ):
            _reject("invalid_zip", "Skill ZIP central directory is malformed")
        name_size, extra_size, comment_size = struct.unpack_from(
            "<3H", archive, cursor + 28
        )
        if name_size > SKILL_ARCHIVE_MAX_PATH_BYTES:
            _reject("path_too_long", "Skill ZIP path exceeds the safety limit")
        cursor += _CENTRAL_FILE_FIXED_BYTES + name_size + extra_size + comment_size
        if cursor > central_end:
            _reject("invalid_zip", "Skill ZIP central directory is truncated")
        count += 1
        if count > SKILL_ARCHIVE_MAX_ENTRIES:
            _reject("too_many_entries", "Skill ZIP contains too many entries")
    if cursor != central_end or count != total_entries:
        _reject("invalid_zip", "Skill ZIP central-directory count is inconsistent")


def _safe_member_parts(name: str) -> tuple[str, ...]:
    if not isinstance(name, str) or not name or "\x00" in name:
        _reject("unsafe_path", "Skill ZIP contains an invalid path")
    if "\\" in name or name.startswith("/"):
        _reject("unsafe_path", "Skill ZIP paths must be relative POSIX paths")
    try:
        encoded_size = len(name.encode("utf-8"))
    except UnicodeEncodeError:
        _reject("unsafe_path", "Skill ZIP path is not valid UTF-8 text")
    if encoded_size > SKILL_ARCHIVE_MAX_PATH_BYTES:
        _reject("path_too_long", "Skill ZIP path exceeds the safety limit")

    raw_parts = name.split("/")
    if raw_parts[-1] == "":
        raw_parts = raw_parts[:-1]
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        _reject("unsafe_path", "Skill ZIP contains an ambiguous path")
    if len(raw_parts) > SKILL_ARCHIVE_MAX_DEPTH:
        _reject("path_too_deep", "Skill ZIP path nesting exceeds the safety limit")
    if raw_parts[0].endswith(":"):
        _reject("unsafe_path", "Skill ZIP contains a drive-qualified path")

    normalized = tuple(unicodedata.normalize("NFC", part) for part in raw_parts)
    if tuple(raw_parts) != normalized:
        _reject("ambiguous_path", "Skill ZIP paths must use NFC Unicode normalization")
    # PurePosixPath is an additional guard against future changes to the raw
    # segment checks above.  It must preserve the exact accepted spelling.
    if PurePosixPath(*normalized).as_posix() != "/".join(normalized):
        _reject("unsafe_path", "Skill ZIP contains an ambiguous path")
    return normalized


def _register_member(
    seen: dict[str, str],
    explicit: set[str],
    parts: tuple[str, ...],
    *,
    is_dir: bool,
) -> None:
    keys = ["/".join(parts[:index]).casefold() for index in range(1, len(parts) + 1)]
    key = keys[-1]
    if key in explicit:
        _reject("duplicate_path", "Skill ZIP contains a duplicate path")
    for parent in keys[:-1]:
        if seen.get(parent) == "file":
            _reject("path_collision", "Skill ZIP path traverses an archived file")
        seen.setdefault(parent, "dir")
    existing = seen.get(key)
    kind = "dir" if is_dir else "file"
    if existing is not None and existing != kind:
        _reject("path_collision", "Skill ZIP contains a file/directory collision")
    if not is_dir and any(candidate.startswith(key + "/") for candidate in seen):
        _reject("path_collision", "Skill ZIP file shadows an archived directory")
    seen[key] = kind
    explicit.add(key)


def _preflight(zf: zipfile.ZipFile) -> tuple[list[zipfile.ZipInfo], SkillArchiveSummary]:
    members = zf.infolist()
    if not members:
        _reject("empty_archive", "Skill ZIP is empty")
    if len(members) > SKILL_ARCHIVE_MAX_ENTRIES:
        _reject("too_many_entries", "Skill ZIP contains too many entries")

    seen: dict[str, str] = {}
    explicit: set[str] = set()
    total_size = 0
    total_compressed = 0
    files = 0
    for info in members:
        if info.orig_filename != info.filename:
            _reject("unsafe_path", "Skill ZIP path contains a NUL byte")
        is_dir = info.is_dir()
        parts = _safe_member_parts(info.filename)
        _register_member(seen, explicit, parts, is_dir=is_dir)
        if len(seen) > SKILL_ARCHIVE_MAX_ENTRIES:
            _reject("too_many_entries", "Skill ZIP expands to too many filesystem entries")

        if info.flag_bits & 0x1:
            _reject("encrypted_entry", "Encrypted Skill ZIP entries are not supported")
        if info.compress_type not in _ALLOWED_COMPRESSION:
            _reject("unsupported_compression", "Skill ZIP uses an unsupported compression method")

        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        if info.create_system == 3:
            if is_dir and file_type not in {0, stat.S_IFDIR}:
                _reject("special_entry", "Skill ZIP directory has an unsafe file type")
            if not is_dir and file_type not in {0, stat.S_IFREG}:
                _reject("special_entry", "Skill ZIP links and special files are not allowed")
        if is_dir:
            if info.file_size != 0:
                _reject("invalid_directory", "Skill ZIP directory carries file data")
            continue

        files += 1
        if info.file_size < 0 or info.compress_size < 0:
            _reject("invalid_size", "Skill ZIP contains an invalid entry size")
        if info.file_size > SKILL_ARCHIVE_MAX_FILE_BYTES:
            _reject("file_too_large", "Skill ZIP contains a file that exceeds the safety limit")
        total_size += info.file_size
        total_compressed += info.compress_size
        if total_size > SKILL_ARCHIVE_MAX_TOTAL_BYTES:
            _reject("archive_too_large", "Skill ZIP expands beyond the total safety limit")
        if (
            info.file_size >= SKILL_ARCHIVE_RATIO_MIN_BYTES
            and (
                info.compress_size == 0
                or info.file_size > info.compress_size * SKILL_ARCHIVE_MAX_RATIO
            )
        ):
            _reject("compression_ratio", "Skill ZIP entry has an unsafe compression ratio")

    if files == 0:
        _reject("empty_archive", "Skill ZIP contains no regular files")
    if (
        total_size >= SKILL_ARCHIVE_RATIO_MIN_BYTES
        and (
            total_compressed == 0
            or total_size > total_compressed * SKILL_ARCHIVE_MAX_RATIO
        )
    ):
        _reject("compression_ratio", "Skill ZIP has an unsafe aggregate compression ratio")
    return members, SkillArchiveSummary(
        entries=len(members),
        files=files,
        uncompressed_bytes=total_size,
        compressed_payload_bytes=total_compressed,
    )


def validate_skill_zip(data: bytes | bytearray | memoryview) -> SkillArchiveSummary:
    """Fully validate a ZIP without writing any member to the filesystem.

    Central-directory budgets are checked before decompression.  Every regular
    member is then streamed to the sink so CRC errors, overlapping entries,
    forged sizes, and truncated compressed streams fail before persistence or
    network forwarding.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("Skill ZIP data must be bytes")
    archive = bytes(data)
    if not archive:
        _reject("empty_archive", "Skill ZIP is empty")
    if len(archive) > SKILL_ARCHIVE_MAX_COMPRESSED_BYTES:
        _reject("compressed_too_large", "Skill ZIP exceeds the compressed size limit")

    try:
        _preflight_central_directory(archive)
        with zipfile.ZipFile(io.BytesIO(archive), mode="r") as zf:
            members, summary = _preflight(zf)
            actual_total = 0
            for info in members:
                if info.is_dir():
                    continue
                actual_file = 0
                with zf.open(info, mode="r") as source:
                    while chunk := source.read(_READ_CHUNK_BYTES):
                        actual_file += len(chunk)
                        actual_total += len(chunk)
                        if actual_file > SKILL_ARCHIVE_MAX_FILE_BYTES:
                            _reject("file_too_large", "Skill ZIP stream exceeds the per-file limit")
                        if actual_total > SKILL_ARCHIVE_MAX_TOTAL_BYTES:
                            _reject("archive_too_large", "Skill ZIP stream exceeds the total limit")
                if actual_file != info.file_size:
                    _reject("size_mismatch", "Skill ZIP entry size does not match its directory record")
            if actual_total != summary.uncompressed_bytes:
                _reject("size_mismatch", "Skill ZIP total size does not match its directory record")
            return summary
    except SkillArchiveValidationError:
        raise
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError, zlib.error) as exc:
        raise SkillArchiveValidationError(
            "invalid_zip",
            "Skill ZIP is corrupt or cannot be safely decoded",
        ) from exc
