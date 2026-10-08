"""Short summaries of a call by the configured small model (``voice.summary_model``).

Mid-call, the summary replaces early turns in the front desk's context; after
hang-up it is saved on the call and greets the user next time. The request
goes the way memory extraction sends its own: the configured provider's
OpenAI-compatible endpoint, one non-streaming completion, no tools. A failure
only means no summary; the call never waits on it longer than its timeout.
"""
import asyncio

import httpx

from core.log import create_logger

log = create_logger("voice.summary")

LIMIT = 300
MAX_TOKENS = 600
MID_CALL_SECONDS, CALL_SECONDS = 15.0, 25.0
_tasks: set[asyncio.Task] = set()  # hang-up summaries outlive the socket

SO_FAR = ("你在帮电话里的语音前台记备忘。把下面的通话记录（和已有备忘）合成一段不超过300字的中文备忘："
          "用户说了什么、交代了什么事、结果怎样、还有什么在办、用户表达的偏好（比如希望怎么称呼）。"
          "只写记录里有的事实，不编造；事情办没办成以“后台备注”里个人助理的回复为准，"
          "前台自己说的“建好了”“办好了”“再试一次”不算数；不写ID、链接和编号；不用列表，直接输出备忘正文。")
CALL = ("下面是用户和个人助理语音前台的一通电话。写一段不超过300字的中文摘要，下次通话开场时给前台看："
        "聊了什么、交代办什么、结果怎样、挂断时还有什么在办、用户表达的偏好（比如希望怎么称呼）。"
        "只写记录里有的事实，不编造；结果以“后台备注”里个人助理的回复为准：前台答应去办、但记录里没有结果的事，"
        "写成“还没有结果”，不要写成已完成或正在执行；前台自己说的“建好了”“办好了”如果后台备注里没有，就当没办成。"
        "不写ID、链接和编号；不用列表，直接输出摘要正文。")


async def summarize(transcript: str, previous: str = "", *, final: bool = False, model: str | None = None,
                    timeout: float | None = None) -> str:
    """The summary text (≤300 characters), or "" when the model is unavailable or says nothing."""
    from core.config import get_config
    if not transcript.strip():
        return previous
    model = model or get_config().voice.summary_model
    body = (f"已有备忘：{previous}\n\n" if previous else "") + "通话记录：\n" + transcript
    try:
        text = await complete(model, CALL if final else SO_FAR, body,
                              timeout or (CALL_SECONDS if final else MID_CALL_SECONDS))
    except Exception as exc:  # no summary is the only consequence
        log.info("voice summary unavailable model=%s final=%s error=%s", model, final, type(exc).__name__)
        return ""
    text = " ".join(text.split())
    return text if len(text) <= LIMIT else text[:LIMIT - 1] + "…"


async def complete(model: str, system: str, text: str, timeout: float) -> str:
    """One plain completion with the model's reasoning turned down as far as it goes."""
    from agent.llm import _get_provider_kwargs, _needs_responses_api, reasoning_profile
    provider = _get_provider_kwargs(model)
    api_key, base_url = provider.get("api_key"), (provider.get("api_base") or "").rstrip("/")
    if not api_key or not base_url:
        raise RuntimeError("provider_not_configured")
    root = base_url if base_url.endswith("/v1") else f"{base_url}/v1"
    bare = model.split("/", 1)[-1]
    effort = next((value for value in ("none", "low", "minimal") if value in reasoning_profile(model).variants), None)
    if _needs_responses_api(model):
        url, payload = f"{root}/responses", {"model": bare, "stream": False, "max_output_tokens": MAX_TOKENS,
                                              "instructions": system, "input": text}
        if effort:
            payload["reasoning"] = {"effort": effort}
    else:
        url, payload = f"{root}/chat/completions", {"model": bare, "stream": False, "max_tokens": MAX_TOKENS,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]}
        if effort:
            payload["reasoning_effort"] = effort  # flat, as the qwen route expects it
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await client.post(url, json=payload, headers={"Authorization": f"Bearer {api_key}"})
    if response.status_code >= 400:
        raise RuntimeError(f"provider_http_{response.status_code}")
    data = response.json()
    if "choices" in data:
        return str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    return str(data.get("output_text") or "".join(
        str(part.get("text") or "") for item in data.get("output") or [] if item.get("type") == "message"
        for part in item.get("content") or [] if part.get("type") == "output_text"))


def save_after_call(call_id: str, transcript: str, previous: str = "", *, summarizer=None) -> asyncio.Task | None:
    """After hang-up: summarize and save on the call, in the background (the user may redial at once)."""
    if not transcript.strip():
        return None

    async def run():
        from voice import calls
        text = await (summarizer or summarize)(transcript, previous, final=True)
        if text:
            try:
                await calls.save_summary(call_id, text)
            except Exception as exc:
                log.warning("voice call=%s summary not saved error=%s", call_id, type(exc).__name__)
                return
        log.info("voice call=%s summary chars=%s", call_id, len(text))
    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task
