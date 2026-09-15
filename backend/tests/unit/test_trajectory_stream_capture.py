"""Provider chunks are recorded verbatim: ``raw`` is the chunk exactly as the adapter received it."""
import json

from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices

from agent.trajectory import RequestCapture, litellm_chunk_blocks, responses_chunk_blocks
from tests.unit.trajectory_producer_support import recording_spool  # noqa: F401
from tool.tool import ToolContext
from trajectory import TraceContext


def _capture() -> RequestCapture:
    return RequestCapture(TraceContext("u1", "s1", turn_id="turn", run_id="run", step_id="step"), None,
                          "chat", "provider/model", "adapter_input")


def test_a_litellm_chunk_keeps_every_provider_field_and_its_text():
    chunk = ModelResponseStream(
        id="chatcmpl-1", created=1757900000, model="m", object="chat.completion.chunk", system_fingerprint="fp_1",
        choices=[StreamingChoices(index=0, delta=Delta(
            content="Hello", reasoning_content="why",
            thinking_blocks=[{"type": "thinking", "thinking": "why", "signature": "sig-1"}],
            provider_specific_fields={"citations": ["https://fixture.invalid"]}))])
    data = _capture().chunk_data(chunk, blocks=litellm_chunk_blocks(chunk))

    assert data["raw"] == chunk.model_dump(mode="json")
    delta = data["raw"]["choices"][0]["delta"]
    assert data["raw"]["system_fingerprint"] == "fp_1"
    assert delta["thinking_blocks"][0]["signature"] == "sig-1"
    assert delta["provider_specific_fields"] == {"citations": ["https://fixture.invalid"]}
    # The raw text stays in the chunk next to the block built from it.
    assert delta["content"] == "Hello" and data["blocks"][0]["delta"] == "Hello"
    assert set(data) == {"chunk_index", "mode", "blocks", "purpose", "raw", "elapsed_ms"}
    assert "$stream_blocks" not in json.dumps(data)


def test_a_responses_event_is_recorded_as_it_arrived_without_a_copy():
    event = {"type": "response.output_item.done", "sequence_number": 7, "output_index": 0,
             "item": {"type": "reasoning", "id": "rs_1", "encrypted_content": "gAAAAB-fixture",
                      "summary": [{"type": "summary_text", "text": "why"}]}}
    data = _capture().chunk_data(event, blocks=responses_chunk_blocks(event))
    assert data["raw"] is event
    assert data["raw"]["item"]["encrypted_content"] == "gAAAAB-fixture"


async def test_a_chunk_is_encoded_when_recorded_so_later_changes_do_not_reach_the_trace(recording_spool):
    ctx = ToolContext(session_id="s1", user_id="u1", workspace_id="w1", message_id="m1",
                      trace_context=TraceContext("u1", "s1", turn_id="turn", run_id="run", step_id="step"))
    capture = await RequestCapture.start(ctx, purpose="chat", model_id="provider/model",
                                         payload={"model": "provider/model"}, capture_level="provider_wire")
    event = {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "Hi", "sequence_number": 3,
             "obfuscation": "x1Y2", "logprobs": [], "blob": b"\x00\x01", "sdk": object()}

    async def provider():
        yield event
        event["delta"] = "changed by the adapter"
    delivered = [chunk async for chunk in capture.stream_chunks(provider(), responses_chunk_blocks)]
    await capture.finish("completed")

    assert delivered == [event]
    [delta] = recording_spool.events("request.delta")
    raw = delta["data"]["raw"]
    assert raw["delta"] == "Hi" and raw["obfuscation"] == "x1Y2" and raw["sequence_number"] == 3
    # Only values JSON cannot hold are replaced, by a marker.
    assert raw["blob"] == {"availability": "not_recorded", "reason": "binary_value"}
    assert raw["sdk"] == {"availability": "not_recorded", "reason": "unsupported_value"}
    assert "raw_content_mode" not in delta["data"]
