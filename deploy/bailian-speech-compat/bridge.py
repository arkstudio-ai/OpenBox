"""New API audio adapter. Run on the gateway's private network, with no host port."""

import asyncio
import io
import json
import math
import re
import wave
from contextlib import asynccontextmanager
from pathlib import PurePath
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from starlette.datastructures import UploadFile

UPSTREAM = "https://dashscope.aliyuncs.com"
ASR_MODEL = "fun-asr"
TTS_MODEL = "qwen-audio-3.1-tts-flash"
MAX_BYTES = 16 * 1024 * 1024
MAX_TEXT = 2000
MEDIA = {"wav": "audio/wav", "mp3": "audio/mpeg", "opus": "audio/ogg", "pcm": "application/octet-stream"}
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".mp4", ".ogg", ".opus", ".aac", ".flac", ".webm", ".mov"}


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        app.state.bailian = client
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
slots = asyncio.Semaphore(4)


@app.get("/health")
def health():
    return {"status": "ok"}


def authorization(request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or not auth[7:].strip():
        raise HTTPException(401, "Provider authorization required")
    return {"Authorization": auth}


def provider_oss_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or parsed.port not in (None, 443)
            or not re.fullmatch(r"dashscope-[a-z0-9-]+\.oss-[a-z0-9-]+\.aliyuncs\.com", host)):
        raise ValueError("Unexpected DashScope storage host")
    return parsed._replace(scheme="https").geturl()


def checked(response):
    if response.status_code == 429:
        raise HTTPException(429, "DashScope rate limit")
    if response.status_code in {401, 403}:
        raise HTTPException(502, "DashScope channel authorization failed")
    if response.status_code == 400:
        raise HTTPException(400, "DashScope rejected audio parameters")
    response.raise_for_status()
    return response


async def request_body(request, limit):
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "Audio request too large")
        chunks.append(chunk)
    return b"".join(chunks)


def playable_wav(audio):
    # DashScope can leave streaming size placeholders in its non-streaming WAV.
    with wave.open(io.BytesIO(audio)) as source:
        params = source.getparams()
        frames = source.readframes(min(params.nframes, len(audio) // (params.sampwidth * params.nchannels)))
    if not frames:
        raise ValueError("Empty WAV")
    out = io.BytesIO()
    with wave.open(out, "wb") as output:
        output.setparams(params)
        output.writeframes(frames)
    return out.getvalue()


@app.post("/v1/audio/speech")
async def speech(request: Request):
    auth = authorization(request)
    try:
        body = json.loads(await request_body(request, 16 * 1024))
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Expected a JSON request")
    if not isinstance(body, dict) or body.get("model") != TTS_MODEL:
        raise HTTPException(400, "Unsupported model")
    text = body.get("input")
    voice = body.get("voice")
    output_format = body.get("response_format") or "mp3"
    language = body.get("language")
    speed = body.get("speed")
    if (not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT
            or not isinstance(voice, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{1,62}", voice)
            or not isinstance(output_format, str) or output_format not in MEDIA
            or body.get("stream_format") == "sse"
            or (language is not None and (not isinstance(language, str) or not re.fullmatch(r"[a-z]{2,3}", language)))
            or (speed is not None and (type(speed) not in {int, float} or not 0.5 <= speed <= 2))):
        raise HTTPException(400, "Unsupported speech input, voice, language, speed, or output format")
    data = {"text": text, "voice": voice, "format": output_format, "sample_rate": 24000}
    if language:
        data["language_hints"] = [language]
    if speed is not None:
        data["rate"] = speed
    client = request.app.state.bailian
    try:
        async with slots:
            response = checked(await client.post(f"{UPSTREAM}/api/v1/services/audio/tts/SpeechSynthesizer",
                                                headers=auth, json={"model": TTS_MODEL, "input": data}))
            audio_url = response.json()["output"]["audio"]["url"]
            audio = checked(await client.get(provider_oss_url(audio_url))).content
            if output_format == "wav":
                audio = playable_wav(audio)
    except (httpx.HTTPError, KeyError, TypeError, ValueError, wave.Error, EOFError):
        raise HTTPException(502, "DashScope speech synthesis failed")
    if not audio:
        raise HTTPException(502, "DashScope returned no audio")
    return Response(audio, media_type=MEDIA[output_format])


async def temporary_input(client, auth, file, suffix):
    response = checked(await client.get(f"{UPSTREAM}/api/v1/uploads", headers=auth,
                                        params={"action": "getPolicy", "model": ASR_MODEL}))
    policy = response.json()["data"]
    filename = f"{uuid4().hex}{suffix}"
    key = f"{policy['upload_dir']}/{filename}"
    fields = {
        "OSSAccessKeyId": policy["oss_access_key_id"], "Signature": policy["signature"],
        "policy": policy["policy"], "x-oss-object-acl": "private",
        "x-oss-forbid-overwrite": "true", "key": key, "success_action_status": "200",
    }
    checked(await client.post(provider_oss_url(policy["upload_host"]), data=fields,
                              files={"file": (filename, file, "application/octet-stream")}))
    return f"oss://{key}"


async def recognize(client, auth, source, language):
    parameters = {"channel_id": [0]}
    if language:
        parameters["language_hints"] = [language]
    headers = {**auth, "X-DashScope-Async": "enable", "X-DashScope-OssResourceResolve": "enable"}
    submitted = checked(await client.post(f"{UPSTREAM}/api/v1/services/audio/asr/transcription",
                        headers=headers, json={"model": ASR_MODEL, "input": {"file_urls": [source]},
                                               "parameters": parameters}))
    task = submitted.json()["output"]["task_id"]
    if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", task):
        raise ValueError("Invalid DashScope task identifier")
    while True:
        response = checked(await client.get(f"{UPSTREAM}/api/v1/tasks/{task}", headers=auth))
        result = response.json()["output"]
        state = result["task_status"]
        if state not in {"PENDING", "RUNNING"}:
            break
        await asyncio.sleep(1)
    item = (result.get("results") or [{}])[0]
    if state != "SUCCEEDED" or item.get("subtask_status", state) != "SUCCEEDED":
        raise ValueError("DashScope transcription did not succeed")
    return checked(await client.get(provider_oss_url(item["transcription_url"]))).json()


def transcription(data, verbose, language):
    tracks = data.get("transcripts", [])
    text = " ".join(track["text"] for track in tracks)
    duration = data["properties"]["original_duration_in_milliseconds"] / 1000
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Missing transcription duration")
    tokens = math.ceil(duration * 1000 / 60)
    result = {"text": text, "duration_ms": round(duration * 1000), "usage": {
        "prompt_tokens": tokens, "total_tokens": tokens,
        "prompt_tokens_details": {"text_tokens": tokens},
    }}
    if verbose:
        sentences = [s for track in tracks for s in track.get("sentences", [])]
        result.update(task="transcribe", duration=duration, language=language or "",
                      segments=[{"id": index, "start": sentence["begin_time"] / 1000,
                                 "end": sentence["end_time"] / 1000, "text": sentence["text"]}
                                for index, sentence in enumerate(sentences)],
                      words=[word for sentence in sentences for word in timed_words(sentence)])
    return result


def timed_words(sentence):
    result = []
    spaced = " " in sentence["text"].strip()
    for part in sentence.get("words", []):
        text, start, end = part["text"], part.get("begin_time", -1), part.get("end_time", -1)
        if end <= start or start < 0 or not text.strip():
            continue
        if spaced and result and not text[0].isspace() and start / 1000 == result[-1]["end"]:
            result[-1]["word"] += text
            result[-1]["end"] = end / 1000
        else:
            result.append({"word": text.strip(), "start": start / 1000, "end": end / 1000})
    return result


@app.post("/v1/audio/transcriptions")
async def transcribe(request: Request):
    auth = authorization(request)
    length = request.headers.get("content-length", "")
    if not length.isdecimal() or int(length) > MAX_BYTES + 65536:
        raise HTTPException(413, "Upload an audio file up to 16 MiB")
    try:
        async with slots, request.form(max_files=1, max_fields=12) as form:
            file = form.get("file")
            language = form.get("language")
            output_format = form.get("response_format") or "json"
            if (form.get("model") != ASR_MODEL or not isinstance(file, UploadFile)
                    or not file.size or file.size > MAX_BYTES or output_format not in {"json", "verbose_json"}
                    or (language is not None and (not isinstance(language, str) or not re.fullmatch(r"[a-z]{2,3}", language)))):
                raise HTTPException(400, "Unsupported audio file, model, language, or response format")
            suffix = PurePath(file.filename or "").suffix.casefold()
            if suffix not in AUDIO_SUFFIXES:
                raise HTTPException(400, "Unsupported audio or video file extension")
            client = request.app.state.bailian
            async with asyncio.timeout(80):
                source = await temporary_input(client, auth, file.file, suffix)
                data = await recognize(client, auth, source, language)
                return transcription(data, output_format == "verbose_json", language)
    except TimeoutError:
        raise HTTPException(504, "DashScope transcription exceeded the time limit")
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        raise HTTPException(502, "DashScope transcription failed")
