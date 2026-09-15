"""Raw chunk slots that repeat a block delta become references; everything else is recorded verbatim."""
from copy import deepcopy
import json

from trajectory.stream_capture import ChunkCapture, block_ids, raw_references

REFERENCE = {"$stream_blocks": ["0:text"], "availability": "stream_reference"}


def capture(raw, blocks, **fields):
    data = {"chunk_index": 1, "mode": "delta", "blocks": blocks, "raw": raw, **fields}
    original = deepcopy(data)
    result = ChunkCapture().capture(data)
    assert data == original
    return result


def test_openai_delta_content_reasoning_and_tool_arguments_reference_their_blocks():
    raw = {"id": "chatcmpl-1", "model": "m", "object": "chat.completion.chunk", "choices": [{"index": 0, "finish_reason": None,
            "delta": {"role": "assistant", "content": "Hello ", "reasoning_content": "thinking",
                      "tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read", "arguments": '{"path":'}}]}}],
           "usage": {"prompt_tokens": 3}}
    blocks = [{"type": "text", "block_id": "0:text", "delta": "Hello "},
              {"type": "reasoning", "block_id": "0:reasoning", "delta": "thinking"},
              {"type": "tool_arguments", "block_id": "tool:0", "tool": "read", "delta": '{"path":'}]
    result = capture(raw, blocks, purpose="chat")
    delta = result["raw"]["choices"][0]["delta"]
    assert delta["content"] == REFERENCE
    assert delta["reasoning_content"] == {"$stream_blocks": ["0:reasoning"], "availability": "stream_reference"}
    assert delta["tool_calls"][0]["function"]["arguments"] == {"$stream_blocks": ["tool:0"], "availability": "stream_reference"}
    assert delta["tool_calls"][0]["function"]["name"] == "read" and delta["role"] == "assistant"
    assert {key: value for key, value in result["raw"].items() if key != "choices"} == {
        "id": "chatcmpl-1", "model": "m", "object": "chat.completion.chunk", "usage": {"prompt_tokens": 3}}
    assert result["blocks"] == blocks and result["blocks"] is blocks
    assert result["raw_content_mode"] == "stream_references" and result["purpose"] == "chat"
    assert result["chunk_index"] == 1 and result["mode"] == "delta"


def test_responses_delta_events_reference_the_item_or_the_single_block():
    named = capture({"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "delta": "Hi"},
                    [{"type": "text", "block_id": "msg_1", "delta": "Hi"}])
    assert named["raw"]["delta"] == {"$stream_blocks": ["msg_1"], "availability": "stream_reference"}
    assert named["raw"]["type"] == "response.output_text.delta" and named["raw"]["item_id"] == "msg_1"
    single = capture({"type": "response.function_call_arguments.delta", "delta": "{}"},
                     [{"type": "tool_arguments", "block_id": "fc_1", "delta": "{}"}])
    assert single["raw"]["delta"] == {"$stream_blocks": ["fc_1"], "availability": "stream_reference"}
    ambiguous = capture({"type": "response.output_text.delta", "item_id": "other", "delta": "Hi"},
                        [{"block_id": "a", "delta": "Hi"}, {"block_id": "b", "delta": ""}])
    assert ambiguous["raw"]["delta"] == "Hi"


def test_completed_response_items_reference_their_replace_blocks():
    raw = {"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "message", "id": "msg_1", "content": [{"type": "output_text", "text": "answer"}]},
        {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "why"}]},
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read", "arguments": "{}"},
        {"type": "function_call", "id": "fc_2", "name": "unknown", "arguments": "{}"}]}}
    blocks = [{"type": "text", "block_id": "msg_1", "delta": "answer", "mode": "replace"},
              {"type": "reasoning", "block_id": "rs_1", "delta": "why", "mode": "replace"},
              {"type": "tool_arguments", "block_id": "fc_1", "delta": "{}", "mode": "replace"}]
    output = capture(raw, blocks)["raw"]["response"]["output"]
    assert output[0]["content"][0]["text"] == {"$stream_blocks": ["msg_1"], "availability": "stream_reference"}
    assert output[1]["summary"][0]["text"] == {"$stream_blocks": ["rs_1"], "availability": "stream_reference"}
    assert output[2]["arguments"] == {"$stream_blocks": ["fc_1"], "availability": "stream_reference"}
    assert output[2]["call_id"] == "call_1" and output[3]["arguments"] == "{}"


def test_unrelated_fields_and_secret_looking_text_pass_through_unchanged():
    raw = {"type": "http.response", "response": {"status": "completed", "output": {
        "task_id": "local_task_123", "url": "https://fixture.invalid/asset?Signature=FIXTURE_SIGNATURE",
        "secret": "sk-FIXTURE_SECRET_VALUE", "authorization": "Bearer FIXTURE_BEARER"}}}
    blocks = [{"type": "text", "block_id": "0:text", "delta": 'api_key="sk-FIXTURE_SECRET_VALUE"'}]
    result = capture(raw, blocks)
    assert result["raw"] == raw and result["raw"] is not raw
    assert result["blocks"][0]["delta"] == 'api_key="sk-FIXTURE_SECRET_VALUE"'
    assert "REDACTED" not in json.dumps(result) and "redaction" not in json.dumps(result)


def test_a_content_slot_without_a_matching_block_keeps_its_text():
    raw = {"choices": [{"index": 1, "delta": {"content": "text", "tool_calls": [{"function": {"arguments": "{}"}}]}},
                       "not a choice"]}
    result = raw_references(raw, [{"type": "text", "delta": "text"}])
    assert result["choices"][0]["delta"]["content"] == "text"
    assert result["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert result["choices"][1] == "not a choice"
    # Blocks without an id are named the way the projector names them.
    assert block_ids([{"type": "text", "delta": "text"}, {"block_id": 7}, "junk"]) == {"text:0", "7"}


def test_non_object_raw_chunks_and_missing_blocks_are_untouched():
    assert raw_references(None, []) is None
    assert raw_references(["a"], [{"block_id": "x"}]) == ["a"]
    result = ChunkCapture().capture({"chunk_index": 2, "raw": {"created": 1, "model": "image"}})
    assert result == {"chunk_index": 2, "raw": {"created": 1, "model": "image"}, "raw_content_mode": "stream_references"}
