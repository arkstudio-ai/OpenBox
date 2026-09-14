"""Current database authority; JWT workspace/role claims never grant access."""
from fastapi import Depends, HTTPException, Request, Response
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.security import HTTPBearer
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import select
from auth.middleware import get_current_user
from db.base import get_db_session
from db.models.user import User
from trajectory.config import admin_enabled


class NoStoreRoute(APIRoute):
    """Observation content and authentication tickets must not enter caches."""
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                response = await original(request)
            except StarletteHTTPException as exc:
                response = await http_exception_handler(request, exc)
            except RequestValidationError as exc:
                response = await request_validation_exception_handler(request, exc)
            response.headers['Cache-Control'] = 'no-store'
            return response
        return handler


async def revalidate_viewer(request: Request, viewer_id: str) -> dict:
    """Recheck revocable authority after potentially slow content I/O."""
    from auth import middleware
    if middleware.is_auth_enabled():
        credentials = await HTTPBearer(auto_error=False)(request)
        identity = await middleware.get_optional_current_user(request, credentials)
        if identity is None or identity['user_id'] != viewer_id:
            raise HTTPException(401, detail='Authentication is no longer valid')
    return await assert_admin(viewer_id)


async def assert_admin(user_id: str) -> dict:
    async with get_db_session() as db:
        row = await db.scalar(select(User).where(User.id == user_id))
        if row is None or row.is_deleted or not row.is_active:
            raise HTTPException(status_code=401, detail="Account is inactive or deleted")
        if row.role != "admin":
            raise HTTPException(status_code=403, detail="Platform administrator access required")
        if not admin_enabled(user_id):
            raise HTTPException(status_code=404, detail="Trajectory administration is disabled")
        return {"user_id": row.id, "role": "admin"}


async def require_trajectory_admin(response: Response, current_user: dict = Depends(get_current_user)) -> dict:
    response.headers['Cache-Control'] = 'no-store'
    return await assert_admin(current_user["user_id"])
