"""The ``invalid`` tool answers instead of raising.

define_tool hands execute() a validated pydantic model; the handler called
``.get`` on it, so any call raised AttributeError.
"""
import pytest

from tool.invalid import invalid_tool


@pytest.mark.asyncio
async def test_invalid_tool_reports_the_missing_tool():
    result = await invalid_tool.execute({"tool": "reed", "error": "no such tool"}, None)
    assert result.title == "Tool not found: reed"
    assert "Tool 'reed' not found" in result.output
    assert "no such tool" in result.output


@pytest.mark.asyncio
async def test_invalid_tool_without_arguments():
    result = await invalid_tool.execute({}, None)
    assert result.title == "Tool not found: unknown"
