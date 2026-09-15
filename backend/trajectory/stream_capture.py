"""Stream references: the recorded chunk keeps each streamed text once.

Block deltas are recorded as the adapter built them. The provider's raw chunk
repeats that text in its own slots (OpenAI ``choices[].delta``, Responses
``delta`` events and ``response.completed`` items); those slots become
references to the blocks of the same event, so a streamed response is not
stored twice. Every other value passes through unchanged.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

RAW_CONTENT_MODE = "stream_references"
STREAM_REFERENCE = "stream_reference"


def block_ids(blocks: list) -> set[str]:
    """Identities as the projector derives them: ``block_id``, else ``{type}:{index}``."""
    return {str(block.get("block_id", f"{block.get('type', 'text')}:{index}"))
            for index, block in enumerate(blocks) if isinstance(block, dict)}


def _reference(text: Any, candidates: list, identifiers: set[str]) -> Any:
    if not isinstance(text, str):
        return text
    mapped = [str(identity) for identity in candidates if str(identity) in identifiers]
    return {"$stream_blocks": mapped, "availability": STREAM_REFERENCE} if mapped else text


def raw_references(value: Any, blocks: list) -> Any:
    """Replace the raw chunk's copies of block text with references; the input is not modified."""
    if not isinstance(value, dict):
        return value
    value = deepcopy(value)
    identifiers = block_ids(blocks)
    for index, choice in enumerate(value.get("choices") or []):
        if not isinstance(choice, dict):
            continue
        choice_id = choice.get("index", index)
        for container in ("delta", "message"):
            body = choice.get(container)
            if not isinstance(body, dict):
                continue
            for field_name, kind in (("content", "text"), ("reasoning_content", "reasoning")):
                if field_name in body:
                    body[field_name] = _reference(body[field_name], [f"{choice_id}:{kind}"], identifiers)
            for call_index, call in enumerate(body.get("tool_calls") or []):
                if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                    continue
                tool_id = call.get("index", call_index)
                function = call["function"]
                if "arguments" in function:
                    function["arguments"] = _reference(
                        function["arguments"], [f"{choice_id}:tool:{tool_id}", f"tool:{tool_id}"], identifiers)
    if isinstance(value.get("delta"), str):
        identity = str(value.get("item_id") or value.get("output_index", 0))
        # A Responses delta names its item; a single block is that item under any name.
        candidates = [identity] if identity in identifiers else list(identifiers) if len(identifiers) == 1 else []
        value["delta"] = _reference(value["delta"], candidates, identifiers)
    response = value.get("response")
    if isinstance(response, dict):
        for index, item in enumerate(response.get("output") or []):
            if not isinstance(item, dict):
                continue
            identity = str(item.get("id") or index)
            if item.get("type") == "function_call" and "arguments" in item:
                item["arguments"] = _reference(item["arguments"], [identity], identifiers)
            if item.get("type") in {"message", "reasoning"}:
                for section in ("content", "summary"):
                    for part in item.get(section) or []:
                        if isinstance(part, dict) and "text" in part:
                            part["text"] = _reference(part["text"], [identity], identifiers)
    return value


class ChunkCapture:
    """Builds the recorded ``request.delta`` data of one provider chunk."""

    raw_content_mode = RAW_CONTENT_MODE

    def capture(self, data: dict) -> dict:
        blocks = data.get("blocks") or []
        return {**data, "raw": raw_references(data.get("raw"), blocks), "raw_content_mode": self.raw_content_mode}
