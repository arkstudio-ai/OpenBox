"""Finite browser actions over Chromium's private file-descriptor transport.

No TCP debugger listener, shell, caller CDP method or JavaScript evaluator is
exposed. An acknowledgement means the finite browser input was delivered,
not that a website completed a business side effect.
"""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import sys


class BrowserPipeError(Exception):
    pass


class BrowserPipe:
    width, height = 1024, 768

    def __init__(self, binary, profile, *, uid=None, gid=None, isolation="chromium_sandbox", fixture_no_sandbox=False, timeout=10):
        if isolation not in {"chromium_sandbox", "container_uid"}:
            raise ValueError("Explicit supported browser isolation is required")
        self.binary, self.profile = str(binary), Path(profile)
        self.uid, self.gid = uid, gid
        self.isolation = isolation
        self.fixture_no_sandbox, self.timeout = fixture_no_sandbox, timeout
        self.process = None
        self._read_transport = self._write = self._reader_task = None
        self._pending, self._events = {}, []
        self._next_id = 0
        self.session_id = self.target_id = None

    @property
    def live(self):
        return self.process is not None and self.process.returncode is None and self._reader_task is not None and not self._reader_task.done()

    async def start(self):
        if self.uid is not None:
            self.profile.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chown(self.profile.parent, self.uid, self.gid)
        else:
            if self.profile.is_symlink():
                raise BrowserPipeError("Symlinked profiles are forbidden")
            self.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        read_child, write_parent = os.pipe()
        read_parent, write_child = os.pipe()
        args = [sys.executable, str(Path(__file__).with_name("browser_pipe_launcher.py")),
                "--binary", self.binary, "--profile", str(self.profile),
                "--read-fd", str(read_child), "--write-fd", str(write_child)]
        if self.uid is not None:
            args.extend(["--uid", str(self.uid), "--gid", str(self.gid)])
        if self.fixture_no_sandbox or self.isolation == "container_uid":
            args.append("--disable-inner-sandbox")
        # Log only browser diagnostics, never inherited backend credentials.
        log_path = self.profile.parent.parent / "browser-stderr.log"
        log = os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab", buffering=0)
        try:
            self.process = await asyncio.create_subprocess_exec(*args, pass_fds=(read_child, write_child),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=log,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin"}, start_new_session=True)
        finally:
            log.close()
            os.close(read_child)
            os.close(write_child)
        self._write = os.fdopen(write_parent, "wb", buffering=0)
        reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
        self._read_transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_parent, "rb", buffering=0))
        self._reader_task = asyncio.create_task(self._read(reader))
        try:
            info = await self.call("Target.getTargets")
            pages = [x for x in info["targetInfos"] if x["type"] == "page"]
            if len(pages) != 1:
                raise BrowserPipeError("A fresh single-page browser profile is required")
            self.target_id = pages[0]["targetId"]
            self.session_id = (await self.call("Target.attachToTarget",
                {"targetId": self.target_id, "flatten": True}))["sessionId"]
            await self.call("Page.enable", session=True)
            await self.call("Page.setLifecycleEventsEnabled", {"enabled": True}, session=True)
            await self.call("Emulation.setDeviceMetricsOverride",
                {"width": self.width, "height": self.height, "deviceScaleFactor": 1, "mobile": False}, session=True)
            await self.call("Browser.setDownloadBehavior", {"behavior": "deny"})
        except BaseException:
            await self.stop()
            raise

    async def _read(self, reader):
        try:
            while True:
                raw = await reader.readuntil(b"\0")
                message = json.loads(raw[:-1])
                if "id" in message:
                    future = self._pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(BrowserPipeError("Browser command was not confirmed"))
                        else:
                            future.set_result(message.get("result", {}))
                elif "method" in message:
                    self._events.append(message)
                    if len(self._events) > 10000:
                        raise BrowserPipeError("Browser event backlog exceeded the finite bound")
        except BaseException:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(BrowserPipeError("Private browser pipe ended"))
            self._pending.clear()

    async def call(self, method, params=None, *, session=False):
        if not self.live:
            raise BrowserPipeError("Private browser runtime unavailable")
        self._next_id += 1
        request_id = self._next_id
        message = {"id": request_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = self.session_id
        payload = json.dumps(message, ensure_ascii=True).encode() + b"\0"
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            # Only the supervisor's serialized dispatcher owns this writer.
            # The durable operation has already entered running before here.
            view = memoryview(payload)
            while view:
                view = view[self._write.write(view):]
            return await asyncio.wait_for(asyncio.shield(future), self.timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise BrowserPipeError("Browser command outcome is unknown") from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Consume a concurrently failed pipe.

    async def _loaded(self, cursor, frame_id, *, loader_id=None, previous_loader=None, history_url=None):
        async with asyncio.timeout(self.timeout):
            while True:
                observed_loader = loader_id
                for event in self._events[cursor:]:
                    if event.get("sessionId") != self.session_id:
                        continue
                    params = event.get("params", {})
                    if event.get("method") == "Page.frameNavigated":
                        frame = params.get("frame", {})
                        if frame.get("id") == frame_id:
                            if history_url and frame.get("url") == history_url and params.get("type") == "BackForwardCacheRestore":
                                return
                            if frame.get("loaderId") != previous_loader:
                                observed_loader = frame.get("loaderId")
                    elif event.get("method") == "Page.navigatedWithinDocument" and history_url:
                        if params.get("frameId") == frame_id and params.get("url") == history_url:
                            return
                    elif event.get("method") == "Page.lifecycleEvent":
                        if (params.get("frameId") == frame_id and params.get("name") == "load"
                                and observed_loader and params.get("loaderId") == observed_loader):
                            return
                if not self.live:
                    raise BrowserPipeError("Browser navigation outcome is unknown")
                await asyncio.sleep(.01)

    async def execute(self, kind, args):
        cursor = len(self._events)
        if kind == "capture":
            frame = (await self.call("Page.getFrameTree", session=True))["frameTree"]["frame"]
            data = (await self.call("Page.captureScreenshot", {"format": "png", "fromSurface": True,
                "captureBeyondViewport": False}, session=True))["data"]
            png = base64.b64decode(data, validate=True)
            if len(png) > 8 * 1024 * 1024 or png[:8] != b"\x89PNG\r\n\x1a\n":
                raise BrowserPipeError("Invalid bounded browser screenshot")
            width, height = struct.unpack(">II", png[16:24])
            return {"png_base64": data, "url": frame["url"], "frame_id": frame["id"],
                    "loader_id": frame.get("loaderId", ""), "sha256": hashlib.sha256(png).hexdigest(),
                    "width": width, "height": height}
        if kind == "navigate":
            result = await self.call("Page.navigate", {"url": args["url"]}, session=True)
            if result.get("errorText"):
                return {"delivered": True, "navigation_error": result["errorText"][:160]}
            if result.get("loaderId"):
                await self._loaded(cursor, result["frameId"], loader_id=result["loaderId"])
        elif kind == "reload":
            frame = (await self.call("Page.getFrameTree", session=True))["frameTree"]["frame"]
            await self.call("Page.reload", {}, session=True)
            await self._loaded(cursor, frame["id"], previous_loader=frame.get("loaderId"))
        elif kind == "back":
            history = await self.call("Page.getNavigationHistory", session=True)
            if history["currentIndex"] == 0:
                return {"delivered": True, "moved": False}
            entry = history["entries"][history["currentIndex"] - 1]
            frame = (await self.call("Page.getFrameTree", session=True))["frameTree"]["frame"]
            await self.call("Page.navigateToHistoryEntry", {"entryId": entry["id"]}, session=True)
            await self._loaded(cursor, frame["id"], previous_loader=frame.get("loaderId"), history_url=entry["url"])
        elif kind == "mouse":
            xy = {"x": args["x"], "y": args["y"]}
            await self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", **xy}, session=True)
            await self.call("Input.dispatchMouseEvent", {"type": "mousePressed", **xy,
                "button": args["button"], "clickCount": 1}, session=True)
            await self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", **xy,
                "button": args["button"], "clickCount": 1}, session=True)
        elif kind == "key":
            keys = {"Enter": 13, "Tab": 9, "Backspace": 8, "Delete": 46, "Escape": 27,
                    "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40,
                    "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34, "Space": 32}
            key = args["key"]
            body = {"key": " " if key == "Space" else key, "code": key, "windowsVirtualKeyCode": keys[key]}
            await self.call("Input.dispatchKeyEvent", {"type": "rawKeyDown", **body}, session=True)
            await self.call("Input.dispatchKeyEvent", {"type": "keyUp", **body}, session=True)
        elif kind == "text":
            await self.call("Input.insertText", {"text": args["text"]}, session=True)
        elif kind == "wheel":
            await self.call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": args["x"],
                "y": args["y"], "deltaX": args["delta_x"], "deltaY": args["delta_y"]}, session=True)
        else:
            raise ValueError("Finite browser operation required")
        return {"delivered": True}

    async def stop(self):
        if self.live:
            try:
                await self.call("Browser.close")
            except (BrowserPipeError, asyncio.TimeoutError):
                pass
        if self._write is not None:
            self._write.close()
        if self.process is not None:
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                # This is only our newly launched process. Its exit is not a
                # descendant-drain proof and never resolves an unknown receipt.
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 2)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        if self._read_transport is not None:
            self._read_transport.close()
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
