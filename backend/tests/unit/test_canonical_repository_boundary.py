"""The legacy repositories must not be a second transcript write path."""

from db.repository.message_repo import PgMessageRepo
from db.repository.part_repo import PgPartRepo


def test_message_repository_is_read_only() -> None:
    assert not hasattr(PgMessageRepo, "create")
    assert not hasattr(PgMessageRepo, "update")


def test_part_repository_is_read_only() -> None:
    assert not hasattr(PgPartRepo, "create")
    assert not hasattr(PgPartRepo, "upsert")
    assert not hasattr(PgPartRepo, "update")
