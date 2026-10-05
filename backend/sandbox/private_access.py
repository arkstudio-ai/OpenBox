"""Session-bound file access and exclusion from legacy shared runtime routes.

Private physical identities are never an authorization token. Ordinary APIs
may use the lookup only to deny an alias; file requests obtain and revalidate
a route from the current persisted Session, actor and workspace instead.
"""
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
import httpx

from assistant.policy import AssistantError

if TYPE_CHECKING:
    from sandbox.private_runtime import PrivateRuntimeRoute


def private_docker_marker(name: str, labels: dict | None = None) -> bool:
    """Reserve the private namespace even when its SQL row is unavailable."""
    return str(name).lstrip("/").startswith("openbox-private-") or any(
        str(key).startswith("openbox.private/") for key in (labels or {})
    )


async def private_container_alias(container_id: str) -> bool:
    if container_id.startswith(("private:", "wpr_")) or private_docker_marker(container_id):
        return True
    from sandbox.private_runtime import find_private_binding
    return await find_private_binding(container_id) is not None


async def require_legacy_container(container_id: str) -> None:
    if await private_container_alias(container_id):
        raise HTTPException(404, "Container not found")


async def legacy_container_request(request: Request) -> None:
    container_id = request.path_params.get("container_id")
    if container_id:
        await require_legacy_container(container_id)


async def public_containers(rows):
    """Keep private names, identities and counts out of shared/admin lists."""
    visible = []
    for row in rows:
        identities = {value for name in ("id", "docker_id", "name")
                      if isinstance(value := getattr(row, name, None), str) and value}
        for identity in identities:
            if await private_container_alias(identity):
                break
        else:
            visible.append(row)
    return visible


def _unavailable(exc: AssistantError) -> HTTPException:
    # Provider exceptions must not put a host, service key or daemon detail in
    # a browser error. The stable code and status remain useful to callers.
    return HTTPException(exc.status, {"code": exc.code, "message": "Private session runtime is unavailable"})


@dataclass(frozen=True)
class SessionFileAccess:
    session_id: str
    user_id: str
    workspace_id: str
    route: "PrivateRuntimeRoute" = field(repr=False)

    @classmethod
    async def resolve(cls, session_id: str, current_user: dict):
        from sandbox.private_runtime import resolve_private_runtime
        user_id = current_user["user_id"]
        workspace_id = current_user["workspace_id"]
        try:
            route = await resolve_private_runtime(session_id=session_id, user_id=user_id,
                                                  workspace_id=workspace_id, create=False)
        except AssistantError as exc:
            raise _unavailable(exc) from None
        if route is None:
            raise HTTPException(404, "Private session runtime not found")
        return cls(session_id, user_id, workspace_id, route)

    async def check(self):
        from sandbox.private_runtime import validate_private_runtime
        try:
            await validate_private_runtime(self.route, session_id=self.session_id, user_id=self.user_id,
                                           workspace_id=self.workspace_id)
        except AssistantError as exc:
            raise _unavailable(exc) from None

    async def post(self, path: str, **kwargs) -> httpx.Response:
        if path not in {"/list_files", "/read_file", "/glob", "/upload"}:
            raise ValueError("Not a private file operation")
        await self.check()
        from sandbox.private_wuying import SCOPE_HEADER, ATTEMPT_HEADER
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False, trust_env=False) as client:
                response = await client.post(self.route.base_url + path,
                    headers={"X-API-Key": self.route.api_key, SCOPE_HEADER: self.route.scope_id,
                             ATTEMPT_HEADER: self.route.guest_attempt_id}, **kwargs)
        except httpx.RequestError:
            raise HTTPException(503, "Private file service is unavailable") from None
        # A response that waited on the runtime cannot reveal bytes after a
        # committed revoke or silently follow a replacement physical binding.
        await self.check()
        if response.status_code >= 300:
            status = response.status_code if 400 <= response.status_code < 500 else 502
            raise HTTPException(status, "Private file operation failed")
        return response
