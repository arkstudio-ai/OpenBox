"""Bound admin reads without admitting an unbounded queue of expensive requests."""
import asyncio
from contextlib import nullcontext

from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeout

from trajectory.auth import NoStoreRoute
from trajectory.config import integer
from trajectory.read_budget import ReadTooLarge, read_budget
from trajectory.worker.metrics import get_metrics


class ReadAdmission:
    def __init__(self):
        self.slots = asyncio.Semaphore(integer("TRAJECTORY_READ_CONCURRENCY", 2))
        self.active = 0
        self.waiting = 0

    def metrics(self):
        metrics = get_metrics()
        metrics.set_gauge("read_active", self.active)
        metrics.set_gauge("read_waiting", self.waiting)

    async def acquire(self) -> bool:
        if self.slots.locked():
            if self.waiting >= integer("TRAJECTORY_READ_MAX_WAITING", 4):
                return False
            self.waiting += 1
            self.metrics()
            try:
                await asyncio.wait_for(self.slots.acquire(),
                                       integer("TRAJECTORY_READ_WAIT_MS", 250) / 1000)
            except TimeoutError:
                return False
            finally:
                self.waiting -= 1
                self.metrics()
        else:
            await self.slots.acquire()
        self.active += 1
        self.metrics()
        return True

    def release(self):
        self.active -= 1
        self.slots.release()
        self.metrics()


class AdmittedResponse(Response):
    """A streamed response keeps its read slot until the stream ends or the client goes away."""

    def __init__(self, response, admission):
        self.response = response
        self.admission = admission
        self.status_code = response.status_code
        self.raw_headers = response.raw_headers
        self.background = response.background

    async def __call__(self, scope, receive, send):
        try:
            await self.response(scope, receive, send)
        finally:
            self.admission.release()


def refused(status: int, code: str, message: str):
    headers = {"Cache-Control": "no-store"}
    if status in (429, 503):
        headers["Retry-After"] = "1"
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status, headers=headers)


class BoundedReadRoute(NoStoreRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            if request.method != "GET":
                return await original(request)
            admission = getattr(request.app.state, "trajectory_read_admission", None)
            if admission is None:
                admission = request.app.state.trajectory_read_admission = ReadAdmission()
            metrics = get_metrics()
            if not await admission.acquire():
                metrics.inc("read_rejected")
                return refused(429, "trajectory_read_busy", "Trajectory viewer is busy; try again shortly")
            handed_off = False
            # Explicit content downloads spool to disk in chunks; allow their
            # full content, while still bounding concurrent transfers and preparation time.
            download = ("/payloads/" in self.path or "/blobs/" in self.path or self.path.endswith("/download"))
            budget = nullcontext() if download else read_budget(integer("TRAJECTORY_READ_MAX_BYTES", 16 * 1024 * 1024))
            timeout = integer("TRAJECTORY_DOWNLOAD_PREPARE_TIMEOUT_SECONDS", 60) if download else integer(
                "TRAJECTORY_READ_TIMEOUT_SECONDS", 10)
            try:
                with budget:
                    async with asyncio.timeout(timeout):
                        response = await original(request)
                    body = getattr(response, "body", None)
                    if body is not None and len(body) > integer("TRAJECTORY_READ_RESPONSE_BYTES", 8 * 1024 * 1024):
                        raise ReadTooLarge("Trajectory result is too large; use a smaller page or download individual content")
                if body is not None:
                    # The body is complete in memory: the slot is released before it is sent, so a slow client
                    # cannot hold it.
                    return response
                handed_off = True
                return AdmittedResponse(response, admission)
            except ReadTooLarge as exc:
                metrics.inc("read_too_large")
                return refused(413, "trajectory_read_too_large", str(exc))
            except (TimeoutError, PoolTimeout):
                metrics.inc("read_timed_out")
                return refused(503, "trajectory_read_timeout", "Trajectory query timed out; try a smaller range")
            except DBAPIError as exc:
                # PostgreSQL statement/lock deadlines are driver errors, not asyncio timeouts.
                if getattr(exc.orig, "sqlstate", None) not in {"57014", "55P03", "25P03"}:
                    raise
                metrics.inc("read_timed_out")
                return refused(503, "trajectory_read_timeout", "Trajectory query timed out; try a smaller range")
            finally:
                if not handed_off:
                    admission.release()

        return handle
