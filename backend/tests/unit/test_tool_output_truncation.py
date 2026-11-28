"""Tool output truncation keeps both ends and never points at a host file.

The full output is saved on the backend host, which the model's file tools
(sandbox only) cannot read, so the model-facing text must not name that file.
"""
import pytest

import tool.truncation as truncation
from tool.truncation import truncate_output


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(truncation, "_data_dir", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_short_output_is_unchanged():
    result = await truncate_output("one\ntwo")
    assert result.content == "one\ntwo"
    assert result.truncated is False
    assert result.output_path is None


@pytest.mark.asyncio
async def test_line_limit_keeps_head_and_tail_without_host_path(_data_dir):
    text = "\n".join(f"line {i}" for i in range(100))
    result = await truncate_output(text, max_lines=10, max_bytes=10_000)

    assert result.truncated is True
    lines = result.content.split("\n")
    assert lines[:8] == [f"line {i}" for i in range(8)]
    assert lines[-2:] == ["line 98", "line 99"]
    assert "line 50" not in result.content
    assert "90 lines" in result.content
    # The full output is still stored, but the model is not sent there.
    assert result.output_path is not None
    assert open(result.output_path, encoding="utf-8").read() == text
    assert str(_data_dir) not in result.content
    assert result.output_path not in result.content
    assert "saved to" not in result.content.lower()


@pytest.mark.asyncio
async def test_byte_limit_keeps_head_and_tail_within_budget():
    text = "\n".join(f"{i:04d}" + "x" * 95 for i in range(1000))
    result = await truncate_output(text, max_lines=5000, max_bytes=2000)

    head, _, tail = result.content.partition("\n\n[...")
    tail = tail.rsplit("...]\n\n", 1)[1]
    assert head.startswith("0000")
    assert tail.endswith("0999" + "x" * 95)
    assert len(head.encode()) + len(tail.encode()) <= 2000
    # Both ends end on whole lines.
    assert all(len(line) == 99 for line in head.split("\n") + tail.split("\n"))


@pytest.mark.asyncio
async def test_single_huge_line_keeps_both_ends():
    text = "BEGIN" + "é" * 100_000 + "END"
    result = await truncate_output(text, max_lines=10, max_bytes=1000)

    assert result.content.startswith("BEGIN")
    assert result.content.endswith("END")
    head, _, rest = result.content.partition("\n\n[...")
    tail = rest.rsplit("...]\n\n", 1)[1]
    assert len(head.encode()) + len(tail.encode()) <= 1000


@pytest.mark.asyncio
async def test_head_direction_keeps_only_the_beginning():
    text = "\n".join(str(i) for i in range(50))
    result = await truncate_output(text, max_lines=5, direction="head")
    assert result.content.startswith("0\n1\n2\n3\n4\n\n[...")
    assert result.content.endswith("...]")
