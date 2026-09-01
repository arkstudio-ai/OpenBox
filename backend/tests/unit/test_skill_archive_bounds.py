"""Personal Skill snapshots have a finite, unambiguous ZIP contract."""

from __future__ import annotations

import io
import stat
import struct
import zipfile

import pytest

import skill.archive as archive_policy
from skill.archive import SkillArchiveValidationError, validate_skill_zip


def _zip(entries, *, compression=zipfile.ZIP_STORED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as bundle:
        for entry, content in entries:
            bundle.writestr(entry, content)
    return output.getvalue()


def _assert_code(blob: bytes, code: str) -> None:
    with pytest.raises(SkillArchiveValidationError) as rejected:
        validate_skill_zip(blob)
    assert rejected.value.code == code


def test_valid_skill_zip_is_crc_streamed_and_summarized():
    blob = _zip([
        ("helper/SKILL.md", "---\nname: helper\ndescription: d\n---\nbody\n"),
        ("helper/references/guide.md", "guide"),
    ], compression=zipfile.ZIP_DEFLATED)

    summary = validate_skill_zip(blob)

    assert summary.entries == 2
    assert summary.files == 2
    assert summary.uncompressed_bytes > 5
    assert summary.compressed_payload_bytes > 0


@pytest.mark.parametrize("path", [
    "../escape/SKILL.md",
    "/absolute/SKILL.md",
    "helper\\SKILL.md",
    "helper//SKILL.md",
    "helper/./SKILL.md",
    "C:/helper/SKILL.md",
    "helper/" + "/".join(["nested"] * 33) + "/SKILL.md",
])
def test_unsafe_or_ambiguous_paths_are_rejected(path):
    code = "path_too_deep" if path.count("/") > 32 else "unsafe_path"
    _assert_code(_zip([(path, "body")]), code)


def test_unicode_and_casefold_aliases_cannot_overwrite_each_other():
    with pytest.warns(UserWarning, match="Duplicate name"):
        exact = _zip([
            ("helper/SKILL.md", "first"),
            ("helper/SKILL.md", "second"),
        ])
    _assert_code(exact, "duplicate_path")

    folded = _zip([
        ("helper/SKILL.md", "first"),
        ("HELPER/skill.md", "second"),
    ])
    _assert_code(folded, "duplicate_path")


def test_file_directory_collisions_are_order_independent():
    _assert_code(
        _zip([
            ("helper/references/guide.md", "guide"),
            ("helper/references", "shadow"),
        ]),
        "path_collision",
    )
    _assert_code(
        _zip([
            ("helper/references", "shadow"),
            ("helper/references/guide.md", "guide"),
        ]),
        "path_collision",
    )


def test_zip_symlinks_and_special_files_are_rejected():
    link = zipfile.ZipInfo("helper/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    _assert_code(_zip([(link, "../../outside")]), "special_entry")


def test_file_total_entry_and_wire_budgets_fail_closed(monkeypatch):
    monkeypatch.setattr(archive_policy, "SKILL_ARCHIVE_MAX_FILE_BYTES", 4)
    _assert_code(_zip([("helper/SKILL.md", "12345")]), "file_too_large")

    monkeypatch.setattr(archive_policy, "SKILL_ARCHIVE_MAX_FILE_BYTES", 10)
    monkeypatch.setattr(archive_policy, "SKILL_ARCHIVE_MAX_TOTAL_BYTES", 6)
    _assert_code(
        _zip([("helper/SKILL.md", "1234"), ("helper/a", "1234")]),
        "archive_too_large",
    )

    monkeypatch.setattr(archive_policy, "SKILL_ARCHIVE_MAX_TOTAL_BYTES", 100)
    monkeypatch.setattr(archive_policy, "SKILL_ARCHIVE_MAX_ENTRIES", 2)
    _assert_code(
        _zip([("one/a", "1"), ("two/b", "2")]),
        "too_many_entries",
    )

    monkeypatch.setattr(archive_policy, "SKILL_ARCHIVE_MAX_ENTRIES", 10)
    blob = _zip([("helper/SKILL.md", "body")])
    monkeypatch.setattr(archive_policy, "SKILL_ARCHIVE_MAX_COMPRESSED_BYTES", len(blob) - 1)
    _assert_code(blob, "compressed_too_large")


def test_high_ratio_zip_bomb_is_rejected_before_inflation(monkeypatch):
    monkeypatch.setattr(archive_policy, "SKILL_ARCHIVE_RATIO_MIN_BYTES", 1)
    monkeypatch.setattr(archive_policy, "SKILL_ARCHIVE_MAX_RATIO", 10)
    bomb = _zip(
        [("helper/SKILL.md", b"0" * 100_000)],
        compression=zipfile.ZIP_DEFLATED,
    )
    _assert_code(bomb, "compression_ratio")


def test_raw_central_directory_count_cannot_hide_object_fanout(monkeypatch):
    blob = bytearray(_zip([
        ("helper/SKILL.md", "body"),
        ("helper/a", "a"),
        ("helper/b", "b"),
    ]))
    eocd = blob.rfind(b"PK\x05\x06")
    assert eocd >= 0
    # Lie in both EOCD counters. The bounded raw scan must count actual central
    # records before zipfile allocates one ZipInfo per attacker-controlled item.
    struct.pack_into("<H", blob, eocd + 8, 1)
    struct.pack_into("<H", blob, eocd + 10, 1)
    monkeypatch.setattr(archive_policy, "SKILL_ARCHIVE_MAX_ENTRIES", 2)
    _assert_code(bytes(blob), "too_many_entries")


def test_crc_corruption_and_truncation_are_rejected():
    blob = _zip([("helper/SKILL.md", b"unique-payload-for-crc")])
    corrupted = bytearray(blob)
    offset = corrupted.index(b"unique-payload-for-crc")
    corrupted[offset] ^= 0x01
    _assert_code(bytes(corrupted), "invalid_zip")
    _assert_code(blob[:-8], "invalid_zip")
