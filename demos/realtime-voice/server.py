"""A localhost-only voice demo, independent of the OpenBox application."""

import asyncio
import base64
import inspect
import json
import os
import re
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
import websockets
from dotenv import dotenv_values
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pricing import CallMeter, PRICE_DATE

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
MODEL = "qwen-audio-3.1-realtime-plus"
VOICE = "longanqian_v3.1"
UPSTREAM_URL = f"wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model={MODEL}"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


def api_key() -> str:
    return os.getenv("DASHSCOPE_API_KEY") or dotenv_values(
        ROOT / "backend/.env", interpolate=False
    ).get("DASHSCOPE_API_KEY") or ""


@app.middleware("http")
async def page_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; worker-src 'self'; media-src 'self' blob:; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
    )
    return response


@app.get("/")
async def index():
    return FileResponse(HERE / "index.html")


@app.get("/app.js")
async def javascript():
    return FileResponse(HERE / "app.js", media_type="application/javascript")


@app.get("/capture-worklet.js")
async def capture_worklet():
    return FileResponse(HERE / "capture-worklet.js", media_type="application/javascript")


@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL, "key_configured": bool(api_key()), "price_date": PRICE_DATE}


def error_message(event: dict) -> str:
    # Never forward raw provider exceptions, headers, or credentials to the browser.
    code = (event.get("error") or {}).get("code", "")
    safe_code = re.sub(r"[^a-zA-Z0-9_.-]", "", str(code))[:80]
    return "百炼返回错误，请重新开始通话。" + (f"（{safe_code}）" if safe_code else "")


async def wait_for_event(upstream, expected: str):
    async with asyncio.timeout(15):
        async for raw in upstream:
            event = json.loads(raw)
            if event.get("type") == "error":
                raise ValueError(error_message(event))
            if event.get("type") == expected:
                return event
    raise ValueError("百炼连接已结束，请重新开始通话。")


async def relay_audio(browser: WebSocket, upstream):
    meter = CallMeter()
    stopping = asyncio.Event()
    response_finished = asyncio.Event()
    await browser.send_json(meter.snapshot())

    async def browser_to_provider():
        while True:
            message = await browser.receive()
            if message["type"] == "websocket.disconnect":
                return
            audio = message.get("bytes")
            if audio is not None:
                if not audio:
                    continue
                if len(audio) > 12800 or len(audio) % 2:
                    await browser.send_json({"type": "error", "message": "音频格式不正确。"})
                    return
                await upstream.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(audio).decode("ascii"),
                }))
            elif message.get("text"):
                # The browser cannot supply provider credentials, URLs, or arbitrary tools.
                if json.loads(message["text"]).get("type") == "stop":
                    stopping.set()
                    if meter.pending:
                        await upstream.send(json.dumps({"type": "response.cancel"}))
                        with suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(response_finished.wait(), 2)
                    return

    async def provider_to_browser():
        active_response = None
        interrupted = False
        async for raw in upstream:
            event = json.loads(raw)
            kind = event.get("type")
            if kind == "error":
                if stopping.is_set():
                    response_finished.set()
                    return
                await browser.send_json({"type": "error", "message": error_message(event)})
                return
            if kind == "input_audio_buffer.speech_started":
                interrupted = True
                meter.pending_input = True
                await browser.send_json(meter.snapshot())
                await browser.send_json({"type": "speech.started"})
            elif kind == "input_audio_buffer.speech_stopped":
                if event.get("reason") == "turn_invalid":
                    meter.pending_input = False
                    await browser.send_json(meter.snapshot())
                await browser.send_json({
                    "type": "speech.stopped", "invalid": event.get("reason") == "turn_invalid",
                })
            elif kind == "response.created":
                active_response = (event.get("response") or {}).get("id")
                meter.start(active_response)
                response_finished.clear()
                await browser.send_json(meter.snapshot())
                interrupted = False
                await browser.send_json({"type": "response.started"})
            elif kind == "response.audio.delta":
                audio = base64.b64decode(event.get("delta", ""), validate=True)
                # Generated audio may be billed even when interruption suppresses playback.
                meter.audio(event.get("response_id") or active_response, audio, event.get("event_id", ""))
                await browser.send_json(meter.snapshot())
                if interrupted:
                    continue
                if event.get("response_id") and event["response_id"] != active_response:
                    continue
                if not stopping.is_set():
                    await browser.send_bytes(audio)
            elif kind in {
                "conversation.item.input_audio_transcription.delta",
                "conversation.item.input_audio_transcription.completed",
            }:
                await browser.send_json({
                    "type": "transcript.user",
                    "text": event.get("delta", event.get("transcript", "")),
                    "final": kind.endswith("completed"),
                })
            elif kind in {"response.audio_transcript.delta", "response.audio_transcript.done"}:
                if interrupted:
                    continue
                if event.get("response_id") and event["response_id"] != active_response:
                    continue
                await browser.send_json({
                    "type": "transcript.assistant",
                    "text": event.get("delta", event.get("transcript", "")),
                    "final": kind.endswith("done"),
                })
            elif kind == "response.done":
                response = event.get("response") or {}
                # Meter old/cancelled responses too, before checking the current playback ID.
                meter.settle(response)
                await browser.send_json(meter.snapshot())
                if not meter.pending:
                    response_finished.set()
                if response.get("id") and response["id"] != active_response:
                    continue
                status = response.get("status", "completed")
                if status == "failed":
                    await browser.send_json({"type": "error", "message": "模型回复失败，请重新开始通话。"})
                    return
                await browser.send_json({"type": "response.done", "cancelled": status == "cancelled"})

    tasks = [asyncio.create_task(browser_to_provider()), asyncio.create_task(provider_to_browser())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        meter.finish()
        with suppress(RuntimeError, WebSocketDisconnect):
            await browser.send_json(meter.snapshot())


@app.websocket("/ws")
async def voice(browser: WebSocket):
    origin = urlsplit(browser.headers.get("origin", ""))
    if origin.scheme != "http" or origin.hostname not in {"127.0.0.1", "localhost"} or origin.netloc != browser.headers.get("host"):
        await browser.close(code=1008)
        return
    await browser.accept()
    key = api_key()
    try:
        if not key:
            await browser.send_json({"type": "error", "message": "请先在 backend/.env 配置 DASHSCOPE_API_KEY。"})
            return
        options = {"open_timeout": 15, "close_timeout": 2, "ping_interval": 20, "max_size": 4 * 1024 * 1024}
        parameters = inspect.signature(websockets.connect).parameters
        options["additional_headers" if "additional_headers" in parameters else "extra_headers"] = {"Authorization": f"Bearer {key}"}
        if "proxy" in parameters:
            options["proxy"] = None
        async with websockets.connect(UPSTREAM_URL, **options) as upstream:
            await wait_for_event(upstream, "session.created")
            await upstream.send(json.dumps({
                "type": "session.update",
                "session": {
                    "modalities": ["text", "audio"],
                    "voice": VOICE,
                    "instructions": "你是 OpenBox 的中文语音助手。请用自然、简短的中文与用户交谈，通常一到三句话。当前是语音通话体验，请不要声称已经执行任何平台操作。",
                    "input_audio_format": "pcm",
                    "output_audio_format": "pcm",
                    "turn_detection": {"type": "smart_turn"},
                },
            }))
            await wait_for_event(upstream, "session.updated")
            await browser.send_json({"type": "ready", "model": MODEL, "output_sample_rate": 24000})
            await relay_audio(browser, upstream)
    except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
        pass
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else "连接百炼失败，请重新开始通话。"
        if key:
            message = message.replace(key, "[已隐藏]")
        with suppress(RuntimeError, WebSocketDisconnect):
            await browser.send_json({"type": "error", "message": message})
    finally:
        with suppress(RuntimeError, WebSocketDisconnect):
            await browser.close()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("OPENBOX_VOICE_DEMO_PORT", "8790")), log_level="warning", access_log=False)
