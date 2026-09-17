#!/usr/bin/env python3
"""Keep a local Wuying forward usable after Session Manager URLs expire.

The CLI caches a WebSocket URL valid for ten minutes, but keeps listening after
it expires. Start and probe a replacement before expiry; existing TCP streams
drain on their original process. Only /alive is probed; user requests are never
replayed. Requires Python 3.12 and ali-instance-cli, no Python dependencies.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
import signal
import socket
import time

log = logging.getLogger("wuying-session-tunnel")


@dataclass(eq=False)
class Generation:
    process: asyncio.subprocess.Process
    port: int
    created: float
    users: int = 0
    retired: bool = False
    stopped: bool = False


class SessionTunnel:
    def __init__(self, command: list[str], *, host="127.0.0.1", port=18002,
                 refresh_after=480.0, probe_interval=30.0, startup_timeout=30.0):
        if command.count("{port}") != 1:
            raise ValueError("command must contain exactly one {port} argument")
        if not 0 < refresh_after <= 540 or min(probe_interval, startup_timeout) <= 0:
            raise ValueError("refresh must be in (0, 540]; timeouts must be positive")
        self.command = command
        self.host, self.port = host, port
        self.refresh_after = refresh_after
        self.probe_interval = probe_interval
        self.startup_timeout = startup_timeout
        self.current: Generation | None = None
        self.generations: set[Generation] = set()
        self.connections: set[asyncio.Task] = set()
        self.server: asyncio.Server | None = None
        self.maintenance: asyncio.Task | None = None

    async def healthy(self, generation: Generation) -> bool:
        if generation.process.returncode is not None:
            return False
        writer = None
        try:
            async with asyncio.timeout(8):
                reader, writer = await asyncio.open_connection("127.0.0.1", generation.port)
                writer.write(b"GET /alive HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                await writer.drain()
                header = await reader.readuntil(b"\r\n\r\n")
                return header.split(b"\r\n", 1)[0].split()[1:2] == [b"200"]
        except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return False
        finally:
            if writer is not None:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()

    async def _stop(self, generation: Generation):
        if generation.stopped:
            return
        if generation.process.returncode is None:
            with suppress(ProcessLookupError):
                generation.process.terminate()
            try:
                await asyncio.wait_for(generation.process.wait(), 5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    generation.process.kill()
                await generation.process.wait()
        generation.stopped = True
        self.generations.discard(generation)

    async def _spawn(self) -> Generation:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        # CLI output may contain signed URLs. Never relay it into application logs.
        process = await asyncio.create_subprocess_exec(
            *(str(port) if part == "{port}" else part for part in self.command),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        generation = Generation(process, port, time.monotonic())
        self.generations.add(generation)
        try:
            async with asyncio.timeout(self.startup_timeout):
                while not await self.healthy(generation):
                    if process.returncode is not None:
                        raise RuntimeError(f"forwarder exited with code {process.returncode}")
                    await asyncio.sleep(.25)
            return generation
        except BaseException:
            await self._stop(generation)
            raise

    async def rotate(self):
        replacement = await self._spawn()
        previous, self.current = self.current, replacement
        if previous is not None:
            previous.retired = True
            log.info("Session renewed; draining %d existing connection(s)", previous.users)
            if previous.users == 0:
                await self._stop(previous)
        else:
            log.info("Session ready")

    async def _maintain(self):
        while True:
            current = self.current
            remaining = 0 if current is None else self.refresh_after - (time.monotonic() - current.created)
            await asyncio.sleep(min(self.probe_interval, max(0, remaining)))
            current = self.current
            expired = current is None or time.monotonic() - current.created >= self.refresh_after
            if expired or not await self.healthy(current):
                try:
                    await self.rotate()
                except (OSError, RuntimeError, TimeoutError) as exc:
                    log.warning("Session renewal failed (%s); retrying after probe interval", type(exc).__name__)
                    await asyncio.sleep(self.probe_interval)

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()

    async def _forward(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        self.connections.add(task)
        generation = self.current
        upstream = None
        if generation is not None:
            generation.users += 1
        try:
            if generation is None:
                return
            remote_reader, upstream = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", generation.port), 8,
            )
            async with asyncio.TaskGroup() as pipes:
                pipes.create_task(self._pipe(reader, upstream))
                pipes.create_task(self._pipe(remote_reader, writer))
        except (OSError, TimeoutError, ExceptionGroup):
            # Once bytes have been forwarded, executing again could duplicate
            # a command. Close this connection; only future connections renew.
            log.warning("TCP connection closed unexpectedly; request was not replayed")
        finally:
            for stream in (writer, upstream):
                if stream is not None:
                    stream.close()
                    with suppress(OSError):
                        await stream.wait_closed()
            if generation is not None:
                generation.users -= 1
                if generation.retired and generation.users == 0:
                    await self._stop(generation)
            self.connections.discard(task)

    async def start(self):
        try:
            await self.rotate()
            self.server = await asyncio.start_server(self._forward, self.host, self.port)
            self.maintenance = asyncio.create_task(self._maintain())
            log.info("Listening on %s:%d; refresh every %.0fs", self.host, self.server.sockets[0].getsockname()[1], self.refresh_after)
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.server is not None:
            self.server.close()
        tasks = list(self.connections)
        if self.maintenance is not None:
            tasks.append(self.maintenance)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.server is not None:
            await self.server.wait_closed()
        await asyncio.gather(*(self._stop(g) for g in list(self.generations)))


async def run(args):
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    tunnel = SessionTunnel(command, host=args.host, port=args.port,
                           refresh_after=args.refresh_after, probe_interval=args.probe_interval)
    stopped = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
    try:
        await tunnel.start()
        await stopped.wait()
    finally:
        await tunnel.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18002)
    parser.add_argument("--refresh-after", type=float, default=480)
    parser.add_argument("--probe-interval", type=float, default=30)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
