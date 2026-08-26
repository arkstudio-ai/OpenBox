"""A quota refusal has to say which quota, and how far over.

All three quotas answered with a bare 429 and prose, so the browser could not
tell "too many conversations" from "too many agents running" — nor either of
those from a model failing. Each now carries a code the client maps to copy and
the two numbers that copy needs.
"""
import pytest
from fastapi import HTTPException

from auth.quota import check_concurrent_agents, check_container_quota, check_session_quota


class _Config:
    max_sessions_per_user = 200
    max_concurrent_agents = 5
    max_containers_per_user = 3


class _Repo:
    def __init__(self, count: int):
        self._count = count

    async def count_by_user(self, user_id: str) -> int:
        return self._count

    async def count_busy(self, user_id: str) -> int:
        return self._count


@pytest.mark.parametrize(
    "check,repo_path,count,code",
    [
        (check_session_quota, "db.repository.session_repo.PgSessionRepo", 241, "SESSION_QUOTA_EXCEEDED"),
        (check_concurrent_agents, "db.repository.session_repo.PgSessionRepo", 5, "CONCURRENT_AGENT_QUOTA_EXCEEDED"),
        (check_container_quota, "db.repository.container_repo.PgContainerRepo", 3, "CONTAINER_QUOTA_EXCEEDED"),
    ],
)
async def test_each_quota_names_itself(monkeypatch, check, repo_path, count, code):
    module, _, attr = repo_path.rpartition(".")
    monkeypatch.setattr(f"{module}.{attr}", lambda: _Repo(count))

    with pytest.raises(HTTPException) as exc:
        await check("u1", _Config())

    assert exc.value.status_code == 429
    detail = exc.value.detail
    assert isinstance(detail, dict), "a bare string cannot be mapped to copy"
    assert detail["code"] == code
    assert detail["used"] == count
    assert detail["limit"] > 0
    assert detail["message"]


async def test_a_quota_under_its_limit_does_not_raise(monkeypatch):
    monkeypatch.setattr("db.repository.session_repo.PgSessionRepo", lambda: _Repo(199))
    await check_session_quota("u1", _Config())


async def test_the_code_also_travels_as_a_header(monkeypatch):
    """So a proxy or a log can classify the refusal without parsing the body."""
    monkeypatch.setattr("db.repository.session_repo.PgSessionRepo", lambda: _Repo(500))
    with pytest.raises(HTTPException) as exc:
        await check_session_quota("u1", _Config())
    assert exc.value.headers["X-Error-Code"] == "SESSION_QUOTA_EXCEEDED"
