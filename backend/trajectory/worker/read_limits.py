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
    """The admission slots of admin reads: at most TRAJECTORY_READ_CONCURRENCY prepare an answer at once, at most
    TRAJECTORY_READ_MAX_WAITING wait for a slot, each for at most TRAJECTORY_READ_WAIT_MS."""

    def __init__(self):
        self.slots = asyncio.Semaphore(self.limit())
        self.active = 0
        self.waiting = 0

    def limit(self) -> int:
        return integer("TRAJECTORY_READ_CONCURRENCY", 2)

    def max_waiting(self) -> int | None:
        return integer("TRAJECTORY_READ_MAX_WAITING", 4)

    def metrics(self):
        metrics = get_metrics()
        metrics.set_gauge("read_active", self.active)
        metrics.set_gauge("read_waiting", self.waiting)

    async def acquire(self) -> bool:
        if self.slots.locked():
            limit = self.max_waiting()
            if limit is not None and self.waiting >= limit:
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


class ReadTransfers(ReadAdmission):
    """The transfer slots of downloads: a download whose content is spooled and revalidated gives its admission
    slot back and streams under one of TRAJECTORY_READ_TRANSFERS instead, so slow clients hold transfer slots,
    never the slots JSON reads need. Waiting for one is bounded by TRAJECTORY_READ_WAIT_MS as for admission."""

    def limit(self) -> int:
        return integer("TRAJECTORY_READ_TRANSFERS", 4)

    def max_waiting(self) -> int | None:
        # Only an admitted download reaches a transfer wait, once: nothing to cap.
        return None

    def metrics(self):
        get_metrics().set_gauge("read_transfers", self.active)


class AdmittedResponse(Response):
    """A streamed response keeps its slot (``slots.release()``) until the stream ends or the client goes away."""

    def __init__(self, response, slots):
        self.response = response
        self.slots = slots
        self.status_code = response.status_code
        self.raw_headers = response.raw_headers
        self.background = response.background

    async def __call__(self, scope, receive, send):
        try:
            await self.response(scope, receive, send)
        finally:
            self.slots.release()


def refused(status: int, code: str, message: str):
    headers = {"Cache-Control": "no-store"}
    if status in (429, 503):
        headers["Retry-After"] = "1"
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status, headers=headers)


def busy():
    get_metrics().inc("read_rejected")
    return refused(429, "trajectory_read_busy", "Trajectory viewer is busy; try again shortly")


def _discard(response) -> None:
    """Release what a streamed response holds when it will never run (``SpooledResponse.close``)."""
    close = getattr(response, "close", None)
    if callable(close):
        close()


async def _transfer(response, transfers: ReadTransfers):
    """The stream under a transfer slot until it ends; 429 with its content released when none frees up in time."""
    try:
        admitted = await transfers.acquire()
    except BaseException:
        _discard(response)
        raise
    if not admitted:
        _discard(response)
        return busy()
    return AdmittedResponse(response, transfers)


def _shared(state, name: str, factory):
    slots = getattr(state, name, None)
    if slots is None:
        slots = factory()
        setattr(state, name, slots)
    return slots


class BoundedReadRoute(NoStoreRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            if request.method != "GET":
                return await original(request)
            admission = _shared(request.app.state, "trajectory_read_admission", ReadAdmission)
            transfers = _shared(request.app.state, "trajectory_read_transfers", ReadTransfers)
            metrics = get_metrics()
            if not await admission.acquire():
                return busy()
            released = False
            # Explicit content downloads spool to disk in chunks; allow their
            # full content, while still bounding concurrent transfers and preparation time.
            download = ("/payloads/" in self.path or "/blobs/" in self.path or self.path.endswith("/download"))
            # The decoded budget defaults to the response cap: content a JSON read could not send anyway is
            # refused before it is fetched and decoded.
            budget = nullcontext() if download else read_budget(integer("TRAJECTORY_READ_MAX_BYTES", 8 * 1024 * 1024))
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
                # A download: its content is spooled and revalidated, so the slot goes back to the JSON reads
                # and the stream runs under a transfer slot instead.
                admission.release()
                released = True
                return await _transfer(response, transfers)
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
                if not released:
                    admission.release()

        return handle
