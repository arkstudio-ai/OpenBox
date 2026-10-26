"""Real image-byte failure: never put binary payloads into model context."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tool.read import ReadArgs, execute
from tool.tool import ToolContext


@pytest.mark.parametrize("path,image", [("/workspace/frame.PNG", True),
    ("/workspace/photo.jpeg", True), ("/workspace/report.pdf", False),
    ("/workspace/source.docx", False), ("/workspace/music.mp3", False)])
async def test_binary_paths_never_start_remote_io(path, image):
    sandbox = SimpleNamespace(read_file=AsyncMock(), execute=AsyncMock())
    result = await execute(ReadArgs(file_path=path), ToolContext(sandbox=sandbox))
    assert result.metadata["failure_code"] == "READ_REQUIRES_BINARY_TOOL"
    assert result.metadata.get("suggested_tool") == ("view_image" if image else None)
    assert "No file content or attachment was returned" in result.output
    sandbox.read_file.assert_not_awaited()
    sandbox.execute.assert_not_awaited()


@pytest.mark.parametrize("body", ["     1\t\ufffdPNG\r\n     2\t\x1a\nraw",
    "     1\t%PDF-1.7\nraw", "     1\tPK\x03\x04raw", "     1\tdata\x00payload"])
async def test_old_server_binary_output_with_wrong_extension_is_not_exposed(body):
    sandbox = SimpleNamespace(read_file=AsyncMock(return_value=body), execute=AsyncMock())
    result = await execute(ReadArgs(file_path="/workspace/data.txt"), ToolContext(sandbox=sandbox))
    assert result.metadata["error"] and body not in result.output
    sandbox.execute.assert_not_awaited()


async def test_unicode_source_text_and_svg_remain_readable(monkeypatch):
    monkeypatch.setattr("session.instruction.instruction_resolve", AsyncMock(return_value=[]))
    body = '     1\t<svg><text>林姐的周末 🌿</text></svg>'
    sandbox = SimpleNamespace(read_file=AsyncMock(return_value=body),
        execute=AsyncMock(return_value=SimpleNamespace(exit_code=0, stdout="123")))
    result = await execute(ReadArgs(file_path="/workspace/chart.svg"), ToolContext(sandbox=sandbox))
    assert result.output == body and not result.metadata.get("error")
    sandbox.execute.assert_awaited_once()


@pytest.mark.parametrize("native", [False, True])
def test_old_binary_read_is_not_replayed_and_audit_input_is_unchanged(native):
    from copy import deepcopy
    import json
    from agent.loop import _to_llm_messages
    blob = "     1\t\ufffdPNG\n     2\t\x00" + "BINARY_SENTINEL" * 2000
    message = SimpleNamespace(id="binary-message", role="assistant", error=None, parts=[{
        "type": "tool", "id": "binary-part", "tool": "read", "status": "completed",
        "input": {"file_path": "/workspace/frame.png"}, "output": blob,
        "state": {"output": blob}, "stream_seq": 1, "call_id": "call_binary"}])
    before = deepcopy(message.parts)
    replay = {message.id: [{"stream_seq": 0, "item": {"type": "tool_search_output",
        "call_id": "search_1", "execution": "server", "status": "completed", "tools": []}}]} if native else None
    wire = json.dumps(_to_llm_messages([message], provider_replay_by_message=replay))
    assert "BINARY_SENTINEL" not in wire and "original audit record is retained" in wire
    assert message.parts == before
