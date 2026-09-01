#!/usr/bin/env python3
"""End-to-end verification for the shared WUYING desktop control path.

Checks legacy-client rejection, cross-client lease serialization, one ordered
desktop action transaction, adaptive final capture, direct OSS upload and OSS
cleanup. It moves the pointer one pixel and restores it; no user files change.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shlex
import time

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from core.config import get_config
from core.oss import get_oss
from sandbox.assets import _use_internal_oss, ensure_cli
from sandbox.client import SandboxClient
from sandbox.desktop import (
    SHOT_PATH,
    ensure_desktop_tools,
    fixed_x,
    take_screenshot,
    take_stable_screenshot,
)


def client() -> SandboxClient:
    config = get_config()
    return SandboxClient(
        host="127.0.0.1",
        port=18000,
        api_key=config.wuying_api_key,
        base_url=config.wuying_endpoint,
    )


async def verify_legacy_rejection() -> int:
    config = get_config()
    async with httpx.AsyncClient(timeout=15, trust_env=False) as http:
        response = await http.post(
            f"{config.wuying_endpoint.rstrip('/')}/execute",
            headers={"X-API-Key": config.wuying_api_key},
            json={
                "command": "PATH=\"$HOME/.local/bin:$PATH\" obx-x xdotool getmouselocation --shell",
                "timeout": 10,
                "workdir": "/workspace",
            },
        )
    if response.status_code != 423:
        raise RuntimeError(f"legacy desktop request was not rejected: HTTP {response.status_code}")
    return response.status_code


async def verify_serialization() -> int:
    first, second = client(), client()

    async def hold() -> None:
        async with first.request_context(
            session_id="verify-holder", tool_call_id="verify-holder", operation="computer"
        ):
            async with first.desktop_lease(
                session_id="verify-holder", tool_call_id="verify-holder"
            ):
                await first.execute("sleep 0.8", timeout=5)

    async def wait() -> int:
        await asyncio.sleep(0.08)
        async with second.request_context(
            session_id="verify-waiter", tool_call_id="verify-waiter", operation="computer"
        ):
            async with second.desktop_lease(
                session_id="verify-waiter", tool_call_id="verify-waiter"
            ) as lease:
                await second.execute("true", timeout=5)
                return int(lease["wait_ms"])

    _, wait_ms = await asyncio.gather(hold(), wait())
    if wait_ms < 600:
        raise RuntimeError(f"desktop leases overlapped; second client waited only {wait_ms}ms")
    return wait_ms


async def verify_desktop_and_oss() -> dict:
    sandbox = client()
    oss = get_oss()
    key = f"verification/openbox-desktop-{int(time.time() * 1000)}.png"
    timings: dict[str, int] = {}

    async with sandbox.request_context(
        session_id="verify-desktop", tool_call_id="verify-desktop", operation="computer"
    ):
        async with sandbox.desktop_lease(
            session_id="verify-desktop", tool_call_id="verify-desktop"
        ) as lease:
            started = time.monotonic()
            await ensure_desktop_tools(sandbox, "verify-wuying-desktop")
            initial = await take_screenshot(sandbox)

            action_started = time.monotonic()
            action = await sandbox.execute(
                fixed_x("xdotool mousemove_relative -- 1 0 mousemove_relative -- -1 0"),
                timeout=20,
            )
            if action.exit_code != 0:
                raise RuntimeError(action.stderr or "desktop action failed")
            timings["execute_ms"] = round((time.monotonic() - action_started) * 1000)

            capture_started = time.monotonic()
            final = await take_stable_screenshot(sandbox)
            timings["settle_capture_ms"] = round((time.monotonic() - capture_started) * 1000)

            await ensure_cli(sandbox, "verify-wuying-desktop")
            upload_started = time.monotonic()
            put_url = oss.presign_put(
                key,
                "image/png",
                expires_sec=300,
                internal=_use_internal_oss(oss),
            )
            uploaded = await sandbox.execute(
                f'PATH="$HOME/.local/bin:$PATH" obx-file put '
                f"{shlex.quote(SHOT_PATH)} {shlex.quote(put_url)} image/png",
                timeout=120,
            )
            if uploaded.exit_code != 0:
                raise RuntimeError(uploaded.stderr or "OSS upload failed")
            head = await oss.head(key)
            timings["oss_ms"] = round((time.monotonic() - upload_started) * 1000)
            timings["total_ms"] = round((time.monotonic() - started) * 1000)

            if not head or int(head.get("size", 0)) != int(final["bytes"]):
                raise RuntimeError(f"OSS verification mismatch: screenshot={final['bytes']} head={head}")

    # Verification assets are not user-visible and should not accumulate.
    delete_url = oss._presign("DELETE", key, expires_sec=120)
    async with httpx.AsyncClient(timeout=15) as http:
        deleted = await http.delete(delete_url)
    if deleted.status_code not in (200, 204):
        raise RuntimeError(f"could not delete verification object: HTTP {deleted.status_code}")

    return {
        "lease_wait_ms": int(lease["wait_ms"]),
        "initial": initial,
        "final": final,
        "oss": head,
        "timings": timings,
        "verification_object_deleted": True,
    }


async def main() -> None:
    result = {
        "legacy_request_status": await verify_legacy_rejection(),
        "serialization_wait_ms": await verify_serialization(),
        "desktop": await verify_desktop_and_oss(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
