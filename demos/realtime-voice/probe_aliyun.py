"""Bounded live probes for Alibaba Cloud Realtime; no OpenBox service is started.

Run with backend/.venv/bin/python. Requests are billable. Only generated test
text/audio is sent; no microphone, existing recordings, or platform tools are
used. Logs redact credentials and omit base64 media. Clone creation is opt-in
and its ID is retained; existing voices are never deleted.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter
from datetime import datetime
import inspect
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import wave
from zoneinfo import ZoneInfo

import requests
import websockets
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
OMNI = "qwen3.8-omni-flash-realtime"
AUDIO = "qwen-audio-3.1-realtime-plus"
VOICES = {OMNI: "Tina", AUDIO: "longanqian_v3.1"}
SAMPLE = "你好，这是一次合成语音测试。今天阳光很好，我们可以慢慢整理计划，先完成最重要的事情，再留一点时间休息。希望你每天都有好心情。"
STYLE_TEXT = "你好，今天我们一起把事情安排好。先完成重要的工作，再留一些时间休息，一步一步来就好。"


class ProviderError(RuntimeError):
    pass


class Probe:
    def __init__(self, model, label, out, key, endpoint):
        self.model, self.label, self.out, self.key = model, label, out, key
        self.endpoint = endpoint
        self.events = []
        self.ws = None
        self.start = time.monotonic()
        self.parts = {}

    def log(self, direction, event):
        event = json.loads(json.dumps(event))
        if event.get("type") in ("response.audio.delta", "input_audio_buffer.append"):
            field = "delta" if direction == "recv" else "audio"
            event[field] = f"<base64 omitted: {len(event.get(field, ''))} characters>"
        # Probe-generated content only; also redact secrets from provider errors.
        safe = json.dumps(event, ensure_ascii=False).replace(self.key, "[REDACTED]")
        self.events.append({"seconds": round(time.monotonic() - self.start, 4),
                            "direction": direction, "event": json.loads(safe)})

    async def __aenter__(self):
        params = inspect.signature(websockets.connect).parameters
        opts = {"open_timeout": 15, "close_timeout": 1, "max_size": 8 * 1024 * 1024}
        opts["additional_headers" if "additional_headers" in params else "extra_headers"] = {
            "Authorization": f"Bearer {self.key}"}
        if "proxy" in params:
            opts["proxy"] = None
        parsed = urlsplit(self.endpoint)
        query = dict(parse_qsl(parsed.query))
        query["model"] = self.model
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
        try:
            self.ws = await websockets.connect(url, **opts)
            await self.until("session.created")
        except BaseException:
            if self.ws:
                await self.ws.close()
            self.flush()
            raise
        return self

    async def __aexit__(self, *args):
        try:
            if self.ws:
                await self.ws.close()
        finally:
            self.flush()

    def flush(self):
        self.out.mkdir(parents=True, exist_ok=True)
        trace = self.out / f"{self.label}.events.json"
        if trace.exists():
            trace = self.out / f"{self.label}.{uuid.uuid4().hex[:8]}.events.json"
        trace.write_text(
            json.dumps(self.events, ensure_ascii=False, indent=2) + "\n")

    async def send(self, kind, **payload):
        event = {"type": kind, "event_id": "probe_" + uuid.uuid4().hex, **payload}
        self.log("send", event)
        await self.ws.send(json.dumps(event, ensure_ascii=False))

    async def recv(self):
        event = json.loads(await self.ws.recv())
        self.log("recv", event)
        if event.get("type") == "response.audio.delta":
            self.parts.setdefault(event.get("response_id", "unknown"), bytearray()).extend(
                base64.b64decode(event["delta"], validate=True))
        return event

    async def until(self, kind, *, timeout=35, allow_error=False):
        async with asyncio.timeout(timeout):
            while True:
                event = await self.recv()
                if event.get("type") == "error":
                    if allow_error:
                        return event
                    raise ProviderError(json.dumps(event.get("error"), ensure_ascii=False)
                                        .replace(self.key, "[REDACTED]"))
                if event.get("type") == kind:
                    return event

    async def configure(self, *, voice=None, modalities=None, turn=None, **extra):
        config = {"modalities": modalities or ["text"], "voice": voice or VOICES[self.model],
                  "instructions": "你是接口测试助手。遵守用户要求，回答简短准确，不编造信息。",
                  "input_audio_format": "pcm", "output_audio_format": "pcm",
                  "turn_detection": turn, **extra}
        await self.send("session.update", session=config)
        return await self.until("session.updated")

    async def item(self, text, *, role="user", item_id=None):
        item = {"type": "message", "role": role,
                "content": [{"type": "input_text", "text": text}]}
        if item_id:
            item["id"] = item_id
        await self.send("conversation.item.create", item=item)
        return (await self.until("conversation.item.created"))["item"]

    async def answer(self, text=None, **response):
        if text is not None:
            await self.item(text)
        start_index = len(self.events)
        started = time.monotonic()
        await self.send("response.create", **({"response": response} if response else {}))
        done = await self.until("response.done", timeout=60)
        return self.summary(done, start_index, started)

    def summary(self, done, start_index, started):
        received = [x for x in self.events[start_index:] if x["direction"] == "recv"]
        response = done.get("response", {})
        rid = response.get("id")
        own = [x for x in received if x["event"].get("response_id", rid) == rid]
        texts = [x["event"].get("text", x["event"].get("transcript", "")) for x in own
                 if x["event"]["type"] in ("response.text.done", "response.audio_transcript.done")]
        pcm = bytes(self.parts.get(rid, b""))
        file = None
        if pcm:
            file = f"{self.label}-{len(list(self.out.glob(self.label + '-*.wav'))):02}.wav"
            with wave.open(str(self.out / file), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm)
        audio_events = [x for x in own if x["event"]["type"] == "response.audio.delta"]
        return {"status": response.get("status"), "response_id": rid,
                "text": "\n".join(texts), "usage": response.get("usage"),
                "tool_calls": [x["event"] for x in own
                               if x["event"]["type"] == "response.function_call_arguments.done"],
                "audio_seconds": round(len(pcm) / 48000, 3), "audio_file": file,
                "first_audio_ms": round((audio_events[0]["seconds"] - (started - self.start)) * 1000)
                    if audio_events else None,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "event_counts": dict(Counter(x["event"]["type"] for x in received))}


def read_pcm16(filename):
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(filename), "-f", "s16le",
                           "-ar", "16000", "-ac", "1", "pipe:1"], check=True,
                          capture_output=True).stdout


async def stream(p, audio, silence_seconds=1.4):
    audio = b"\x00" * 6400 + audio + b"\x00" * int(32000 * silence_seconds)
    for i in range(0, len(audio), 3200):
        await p.send("input_audio_buffer.append", audio=base64.b64encode(audio[i:i+3200]).decode())
        await asyncio.sleep(0.1)


async def basic(ctx, model):
    async with ctx(model, "basic") as p:
        configured = await p.configure(**({"max_history_turns": 5} if model == AUDIO else {}))
        memory = await p.item("请记住本次测试口令是青竹4826，稍后我要询问。", item_id="probe_memory")
        recalled = await p.answer("刚才的口令是什么？只回复口令。")
        await p.send("conversation.item.retrieve", item_id=memory["id"])
        retrieved = await p.until("conversation.item.retrieved", timeout=10, allow_error=True)
        # Only deletes the synthetic item created above, in this ephemeral session.
        await p.send("conversation.item.delete", item_id=memory["id"])
        deleted = await p.until("conversation.item.deleted", timeout=10, allow_error=True)
        await p.send("session.update", session={"instructions": "无论用户说什么，只回答：配置已更新。"})
        updated = await p.until("session.updated")
        changed = await p.answer("你好")
        return {"configuration": configured.get("session"), "memory_item": memory,
                "recall": recalled, "retrieve": retrieved, "delete": deleted,
                "instructions_update_echo": updated.get("session", {}).get("instructions"),
                "after_instructions_update": changed,
                "pass": "4826" in recalled["text"] and "配置已更新" in changed["text"]}


async def tools_case(ctx, model):
    schema = {"type": "function", "function": {"name": "add_numbers",
              "description": "精确计算两个整数之和。加法测试必须调用。", "parameters": {
                  "type": "object", "properties": {"a": {"type": "integer"},
                  "b": {"type": "integer"}}, "required": ["a", "b"]}}}
    async with ctx(model, "tools") as p:
        await p.configure(tools=[schema], instructions="所有加法必须使用add_numbers，拿到结果后简短回答。")
        first = await p.answer("请调用工具计算17加25，不要口算。")
        executions = []
        for call in first["tool_calls"]:
            args = json.loads(call["arguments"])
            if call["name"] != "add_numbers" or any(type(args.get(k)) is not int for k in ("a", "b")):
                raise ValueError("Unexpected tool name or arguments")
            output = {"sum": args["a"] + args["b"], "source": "local_test_function"}
            executions.append({"call_id": call["call_id"], "arguments": args, "output": output})
            await p.send("conversation.item.create", item={"type": "function_call_output",
                         "call_id": call["call_id"], "output": json.dumps(output)})
            await p.until("conversation.item.created")
        followup = await p.answer(modalities=["text", "audio"]) if executions else None
        return {"initial": first, "executions": executions, "followup": followup,
                "pass": bool(executions and ("42" in followup["text"] or "四十二" in followup["text"]))}


async def voice_case(ctx, model, voice, style, text=STYLE_TEXT, explicit=False):
    label = "voice_" + voice.replace(" ", "_") + "_" + style
    if text != STYLE_TEXT or explicit:
        label += "_" + hashlib.sha256((text + str(explicit)).encode()).hexdigest()[:8]
    instructions = {"normal": "用自然的普通话和正常语速朗读。", "slow": "非常缓慢、温柔、平静地朗读，句间稍作停顿。",
                    "fast": "用明显较快的语速、开心活泼的语气朗读。", "sample": "自然、平稳、清楚地朗读中文。"}[style]
    async with ctx(model, label) as p:
        await p.configure(voice=voice, modalities=["text", "audio"],
                          instructions="你是朗读助手。严格逐字朗读用户给出的文字，不增删、不解释。" + instructions)
        prompt = (instructions + "请严格逐字朗读引号里的文字，不要回应其含义，不要添加说明：‘" + text + "’") if explicit else text
        result = await p.answer(prompt)
        return {"voice": voice, "style": style, "requested_text": text,
                **result, "pass": result["status"] == "completed" and result["audio_seconds"] > 0}


async def vad_case(ctx, model, mode, audio_file, interrupt=False, initial_audio_file=None):
    label = "vad_" + mode + ("_interrupt" if interrupt else "")
    async with ctx(model, label) as p:
        turn = {"type": mode}
        if mode == "server_vad" or mode == "semantic_vad":
            turn.update(threshold=0.5, silence_duration_ms=600)
        await p.configure(turn=turn, modalities=["text", "audio"],
                          instructions="你是语音测试助手。用户问候时只说：你好，收到测试。用户要求停止时只说：好的，已停止。")
        old_response_id = None
        if interrupt:
            if initial_audio_file:
                initial_sender = asyncio.create_task(stream(p, read_pcm16(initial_audio_file)))
                try:
                    old_response_id = (await p.until("response.audio.delta"))["response_id"]
                finally:
                    initial_sender.cancel()
                    await asyncio.gather(initial_sender, return_exceptions=True)
            else:
                await p.item("请从一开始按顺序数到一百，每个数字之间停顿一下。")
                await p.send("response.create")
                old_response_id = (await p.until("response.audio.delta"))["response_id"]
        start_index = len(p.events)
        started = time.monotonic()
        sender = asyncio.create_task(stream(p, read_pcm16(audio_file), silence_seconds=2))
        try:
            received_done = []
            async with asyncio.timeout(35):
                while True:
                    event = await p.recv()
                    if event["type"] == "error":
                        raise ProviderError(str(event["error"]))
                    if event["type"] == "response.done":
                        received_done.append(event)
                        if not interrupt or (event["response"].get("status") == "completed"
                                and event["response"].get("id") != old_response_id):
                            break
            await sender
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        summary = p.summary(received_done[-1], start_index, started)
        speech = [x for x in p.events[start_index:] if x["direction"] == "recv" and
                  x["event"]["type"] in ("input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped")]
        statuses = [e["response"].get("status") for e in received_done]
        return {"mode": mode, "interruption": interrupt, "audio_triggered_initial_response": bool(initial_audio_file), "speech_events": speech,
                "response_statuses": statuses, "result": summary,
                "pass": len(speech) >= 2 and (not interrupt or "cancelled" in statuses)}


async def clone_case(ctx, model, out, key, http_endpoint, upload_reference=False):
    if model != OMNI:
        return await audio_clone_case(ctx, out, key, http_endpoint, upload_reference)
    record = out / "created-clone.json"
    if record.exists():
        data = json.loads(record.read_text())
    else:
        samples = sorted(out.glob("omni_voice_Tina_sample*-*.wav"))
        if not samples:
            raise ValueError("Run --phase voices to generate the synthetic reference first")
        with wave.open(str(samples[-1])) as w:
            duration = w.getnframes() / w.getframerate()
            if not 3 <= duration <= 60 or w.getframerate() < 24000:
                raise ValueError("Reference is outside the documented audio requirements")
        payload = {"model": "qwen-voice-enrollment", "input": {"action": "create",
                   "target_model": model, "preferred_name": "openbox_probe",
                   "audio": {"data": "data:audio/wav;base64," + base64.b64encode(samples[-1].read_bytes()).decode()},
                   "language": "zh"}, "parameters": {"voice_clone_mode": "normal"}}
        response = await asyncio.to_thread(requests.post, http_endpoint + "/api/v1/services/audio/tts/customization",
                         json=payload, headers={"Authorization": "Bearer " + key}, timeout=60)
        data = response.json()
        if response.status_code != 200 or not data.get("output", {}).get("voice"):
            raise ProviderError(json.dumps({"status": response.status_code, "body": data}, ensure_ascii=False))
        record.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    voice = data["output"]["voice"]
    result = await voice_case(ctx, model, voice, "normal", "你好，这是合成测试音频创建的复刻音色，接口验证已经完成。")
    response = await asyncio.to_thread(requests.post, http_endpoint + "/api/v1/services/audio/tts/customization",
                    json={"model": "qwen-voice-enrollment", "input": {"action": "list", "page_size": 10, "page_index": 0}},
                    headers={"Authorization": "Bearer " + key}, timeout=30)
    listing = response.json()
    found = any(x.get("voice") == voice for x in listing.get("output", {}).get("voice_list", []))
    return {"creation": data, "source": "synthetic Tina sample generated by this probe",
            "voice_retained": True, "list_http_status": response.status_code, "found_in_first_page": found,
            "synthesis": result, "pass": result["pass"] and found}


async def audio_clone_case(ctx, out, key, http_endpoint, upload_reference):
    record = out / "created-audio-clone.json"
    if record.exists():
        creation = json.loads(record.read_text())
    else:
        if not upload_reference:
            return {"skipped": "Use --upload-reference to upload this probe's synthetic WAV to configured OSS."}
        from dotenv import load_dotenv
        load_dotenv(ROOT / "backend/.env", override=False)
        sys.path.insert(0, str(ROOT / "backend"))
        from core.oss import get_oss
        samples = sorted(out.glob("omni_voice_Tina_sample*-*.wav"))
        if not samples:
            raise ValueError("Run --phase voices first")
        data = samples[-1].read_bytes()
        object_key = "research/aliyun-realtime/20261010/" + hashlib.sha256(data).hexdigest()[:20] + "/synthetic-reference.wav"
        oss = get_oss()
        await oss.put_object(object_key, data, content_type="audio/wav", forbid_overwrite=True, timeout=30)
        reference_url = oss.presign_get(object_key, expires_sec=600)
        # A short-lived signed URL is only sent to Alibaba Cloud. Never write it to logs.
        response = await asyncio.to_thread(requests.post, http_endpoint + "/api/v1/services/audio/tts/customization",
            json={"model": "voice-enrollment", "input": {"action": "create_voice", "target_model": AUDIO,
                  "prefix": "obprobe", "url": reference_url}}, headers={"Authorization": "Bearer " + key}, timeout=60)
        creation = response.json()
        if response.status_code != 200 or not creation.get("output", {}).get("voice_id"):
            raise ProviderError(json.dumps(creation, ensure_ascii=False).replace(reference_url, "[signed URL]"))
        creation["reference_object_key"] = object_key
        record.write_text(json.dumps(creation, ensure_ascii=False, indent=2) + "\n")
    voice = creation["output"]["voice_id"]
    query = None
    for _ in range(10):
        response = await asyncio.to_thread(requests.post, http_endpoint + "/api/v1/services/audio/tts/customization",
            json={"model": "voice-enrollment", "input": {"action": "query_voice", "voice_id": voice}},
            headers={"Authorization": "Bearer " + key}, timeout=20)
        query = response.json()
        query.get("output", {}).pop("resource_link", None)
        if query.get("output", {}).get("status") in ("OK", "UNDEPLOYED", "DISABLED"):
            break
        await asyncio.sleep(2)
    if query.get("output", {}).get("status") != "OK":
        return {"creation": creation, "query": query, "pass": False}
    synthesis = await voice_case(ctx, AUDIO, voice, "normal", "你好，这是合成测试素材创建的语音对话音色。", explicit=True)
    return {"creation": creation, "query": query, "synthesis": synthesis,
            "source": "synthetic Tina sample; a single OSS reference object is retained",
            "voice_retained": True, "pass": synthesis["pass"]}


async def context_case(ctx, model):
    async with ctx(model, "context_prune") as p:
        await p.configure()
        await p.item("本次合成测试中，我喜欢青色，项目代号为海棠73。")
        recalled = await p.answer("我喜欢什么颜色，项目代号是什么？")
        ids = list(dict.fromkeys(x["event"]["item"]["id"] for x in p.events
                   if x["direction"] == "recv" and x["event"]["type"] == "conversation.item.created"))
        removed = []
        for item_id in ids:
            await p.send("conversation.item.delete", item_id=item_id)
            removed.append(await p.until("conversation.item.deleted", allow_error=True))
        after = await p.answer("我之前告诉你的喜欢的颜色是什么？如果没有上下文，请只回答不知道。")
    async with ctx(model, "context_restore") as p:
        await p.configure()
        await p.item("历史对话摘要：用户喜欢青色，项目代号海棠73。此摘要来自应用保存的测试记忆。", role="system")
        restored = await p.answer("我喜欢什么颜色，项目代号是什么？")
    return {"before_pruning": recalled, "removed": removed, "after_pruning": after,
            "new_session_with_summary": restored,
            "pass": "不知道" in after["text"] and "海棠73" in restored["text"].replace(" ", "")}


async def manual_case(ctx, model, audio_file):
    async with ctx(model, "manual_audio") as p:
        extra = {"input_audio_transcription": {"model": "qwen3-asr-flash-realtime"}} if model == OMNI else {
            "input_audio_transcription": {"language": "zh"}, "output_audio": {"language": "zh"},
            "enable_speech_emotion": True}
        configured = await p.configure(modalities=["text", "audio"], **extra)
        raw = read_pcm16(audio_file)
        for i in range(0, len(raw), 3200):
            await p.send("input_audio_buffer.append", audio=base64.b64encode(raw[i:i+3200]).decode())
        await p.send("input_audio_buffer.commit")
        await p.until("input_audio_buffer.committed")
        response = await p.answer()
        transcripts = [x["event"] for x in p.events if x["direction"] == "recv" and
                       x["event"]["type"] == "conversation.item.input_audio_transcription.completed"]
        return {"configuration": configured.get("session"), "transcripts": transcripts,
                "result": response, "pass": response["audio_seconds"] > 0}


async def cancel_case(ctx, model):
    async with ctx(model, "cancel") as p:
        await p.configure(modalities=["text", "audio"])
        await p.item("请按顺序从一数到一百，把每一个数字都念出来。")
        await p.send("response.create")
        first = await p.until("response.audio.delta")
        await p.send("response.cancel")
        done = await p.until("response.done")
        await p.send("conversation.item.truncate", item_id=first["item_id"], content_index=0, audio_end_ms=100)
        try:
            truncated = await p.until("conversation.item.truncated", timeout=5, allow_error=True)
        except TimeoutError:
            truncated = {"observation": "No acknowledgement or error within 5 seconds; support is unverified."}
        return {"cancelled_response": done["response"], "truncate_result": truncated,
                "pass": done["response"].get("status") == "cancelled"}


async def boundary_case(ctx, model):
    async with ctx(model, "config_boundaries") as p:
        await p.configure(modalities=["text", "audio"])
        first = await p.answer("只说你好。")
        alternate = "Andre" if model == OMNI else "longanhuan_v3.1"
        await p.send("session.update", session={"voice": alternate})
        updated = await p.until("session.updated", allow_error=True)
        second = await p.answer("只说第二次测试。")
        await p.send("session.update", session={"tools": [{"type": "function", "function": {
                     "name": "noop", "parameters": {"type": "object", "properties": {}}}}],
                     "enable_search": True})
        incompatible = await p.until("session.updated", timeout=10, allow_error=True)
        return {"first": first, "voice_update": updated, "second": second,
                "tools_and_search_configuration": incompatible,
                "note": "A session.updated echo alone does not prove acoustic or combined tool/search support."}


async def search_case(ctx, model):
    async with ctx(model, "web_search") as p:
        await p.configure(enable_search=True, search_options={"enable_source": True})
        result = await p.answer("请联网查找阿里云官方Qwen实时语音对话文档地址，返回一条官网链接即可。")
        sources = [x["event"]["response"]["search_info"] for x in p.events if x["direction"] == "recv" and
                   x["event"].get("response", {}).get("search_info")]
        search_count = (result.get("usage") or {}).get("plugins", {}).get("search", {}).get("count", 0)
        return {"result": result, "search_events": sources,
                "search_evidence": bool(sources or search_count), "search_count": search_count,
                "note": "Search is verified using provider search_info and usage.plugins.search."}


async def mcp_case(ctx):
    async with ctx(OMNI, "mcp") as p:
        await p.configure(tools=[{"type": "mcp", "server_label": "cloudflare_docs",
                          "server_url": "https://docs.mcp.cloudflare.com/mcp", "require_approval": "never"}])
        async with asyncio.timeout(40):
            while not any(x["event"]["type"] in ("mcp_list_tools.completed", "mcp_list_tools.failed") for x in p.events):
                event = await p.recv()
                if event["type"] == "error":
                    raise ProviderError(str(event["error"]))
        discovery = [x["event"] for x in p.events if x["direction"] == "recv" and
                     (x["event"]["type"].startswith("mcp_list_tools") or
                     x["event"].get("item", {}).get("type") == "mcp_list_tools")]
        if any(x["type"] == "mcp_list_tools.failed" for x in discovery):
            return {"discovery": discovery, "pass": False}
        first = await p.answer("请调用cloudflare_docs工具查找Workers是什么，只用一句中文回答。")
        mcp = [x["event"] for x in p.events if x["direction"] == "recv" and
               x["event"]["type"].startswith("response.mcp_")]
        completed = any(x["type"] == "response.mcp_call.completed" for x in mcp)
        followup = await p.answer() if completed else None
        return {"discovery": discovery, "initial": first, "mcp_events": mcp,
                "followup": followup, "pass": completed}


async def tts_case(ctx, label, text=STYLE_TEXT, **options):
    model = "qwen3-tts-instruct-flash-realtime"
    async with ctx(model, "tts_" + label) as p:
        config = {"voice": "Cherry", "mode": "commit", "language_type": "Chinese",
                  "response_format": "pcm", "sample_rate": 24000, **options}
        await p.send("session.update", session=config)
        updated = await p.until("session.updated")
        start_index, started = len(p.events), time.monotonic()
        await p.send("input_text_buffer.append", text=text)
        await p.send("input_text_buffer.commit")
        done = await p.until("response.done", timeout=40)
        result = p.summary(done, start_index, started)
        await p.send("session.finish")
        finished = await p.until("session.finished", timeout=10)
        return {"configuration": updated.get("session"), "requested": config, "requested_text": text,
                "result": result, "finished": finished, "pass": result["audio_seconds"] > 0}


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase", choices=["basic", "tools", "voices", "style", "vad", "clone", "extra", "tts"], required=True)
    ap.add_argument("--case", help="Run only a named case in the selected phase")
    ap.add_argument("--upload-reference", action="store_true", help="For Audio cloning, upload generated reference WAV to configured OSS")
    ap.add_argument("--model", choices=["omni", "audio", "both"], default="both")
    ap.add_argument("--out", type=Path, default=ROOT / "test-results/aliyun-realtime-20261010")
    args = ap.parse_args()
    env = {**dotenv_values(ROOT / "backend/.env", interpolate=False), **os.environ}
    key = env.get("DASHSCOPE_API_KEY")
    if not key:
        raise SystemExit("DASHSCOPE_API_KEY is missing")
    workspace = env.get("VOICE_WORKSPACE_ID")
    host = f"{workspace}.cn-beijing.maas.aliyuncs.com" if workspace else "dashscope.aliyuncs.com"
    endpoint = env.get("REALTIME_WS_URL") or f"wss://{host}/api-ws/v1/realtime"
    args.out.mkdir(parents=True, exist_ok=True)
    summary_file = args.out / "results.json"
    summary = json.loads(summary_file.read_text()) if summary_file.exists() else {
        "started_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "endpoint": endpoint, "cases": {}, "scope": "Synthetic fixtures; no microphone or platform actions"}
    models = [OMNI, AUDIO] if args.model == "both" else [{"omni": OMNI, "audio": AUDIO}[args.model]]
    def ctx(model, label):
        prefix = "omni" if model == OMNI else "audio"
        return Probe(model, prefix + "_" + label, args.out, key, endpoint)
    async def run(name, awaitable):
        if args.case and args.case != name:
            awaitable.close()
            return
        print("RUN", name, flush=True)
        try:
            result = await awaitable
        except Exception as exc:
            result = {"pass": False, "error_type": type(exc).__name__, "error": str(exc).replace(key, "[REDACTED]")}
        if name in summary["cases"]:
            summary.setdefault("previous_attempts", []).append({"case": name, "result": summary["cases"][name]})
        summary["cases"][name] = result
        summary_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"case": name, "pass": result.get("pass", "recorded"),
                          "error": result.get("error"), "audio_file": result.get("audio_file")}, ensure_ascii=False), flush=True)
    for model in models:
        prefix = "omni" if model == OMNI else "audio"
        if args.phase == "basic":
            await run(prefix + "_basic", basic(ctx, model))
        elif args.phase == "tools":
            await run(prefix + "_tools", tools_case(ctx, model))
        elif args.phase == "voices":
            for style in ("normal", "slow", "fast"):
                await run(prefix + "_voice_" + style, voice_case(ctx, model, VOICES[model], style))
            other = "Andre" if model == OMNI else "longanhuan_v3.1"
            await run(prefix + "_voice_alternate", voice_case(ctx, model, other, "normal"))
            if model == OMNI:
                for style, text, label in (("sample", SAMPLE, "sample"),
                    ("normal", "你好，请问你能听见我说话吗？", "vad_fixture"),
                    ("normal", "请停一下，我现在想换个话题。", "interrupt_fixture")):
                    await run(prefix + "_" + label, voice_case(ctx, model, "Tina", style, text))
        elif args.phase == "style":
            for style in ("normal", "slow", "fast"):
                await run(prefix + "_explicit_" + style, voice_case(ctx, model, VOICES[model], style, explicit=True))
            if model == OMNI:
                for text, label in (("你好，请问你能听见我说话吗？", "vad_fixture"),
                                    ("请停一下，我现在想换个话题。", "interrupt_fixture")):
                    await run(prefix + "_" + label, voice_case(ctx, model, "Tina", "normal", text, explicit=True))
                await run("omni_long_request_fixture", tts_case(ctx, "long_request_fixture", text="请从一数到一百，把每个数字都念出来。"))
        elif args.phase == "vad":
            def fixture(name):
                entry = summary["cases"]["omni_" + name]
                return args.out / (entry.get("audio_file") or entry["result"]["audio_file"])
            modes = ["server_vad", "semantic_vad" if model == OMNI else "smart_turn"]
            for mode in modes:
                await run(prefix + "_vad_" + mode, vad_case(ctx, model, mode, fixture("vad_fixture")))
            initial_audio = fixture("long_request_fixture") if "omni_long_request_fixture" in summary["cases"] else None
            await run(prefix + "_interrupt", vad_case(ctx, model, modes[-1], fixture("interrupt_fixture"), True, initial_audio))
        elif args.phase == "clone":
            await run(prefix + "_clone", clone_case(ctx, model, args.out, key, f"https://{host}", args.upload_reference))
        elif args.phase == "extra":
            await run(prefix + "_context_prune_restore", context_case(ctx, model))
            await run(prefix + "_manual_audio", manual_case(ctx, model, args.out / summary["cases"]["omni_vad_fixture"]["audio_file"]))
            await run(prefix + "_cancel_truncate", cancel_case(ctx, model))
            await run(prefix + "_config_boundaries", boundary_case(ctx, model))
            await run(prefix + "_web_search", search_case(ctx, model))
            if model == OMNI:
                await run("omni_mcp", mcp_case(ctx))
        elif args.phase == "tts":
            for label, options in (("normal", {"speech_rate": 1.0}), ("slow", {"speech_rate": 0.7}),
                    ("fast", {"speech_rate": 1.3}), ("pitch_volume", {"pitch_rate": 0.8, "volume": 30}),
                    ("emotion", {"instructions": "语气开心活泼，带有明显笑意。", "optimize_instructions": True})):
                await run("tts_" + label, tts_case(ctx, label, **options))
            break


if __name__ == "__main__":
    asyncio.run(main())
