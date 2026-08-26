"""A failed MCP call must always say something the reader can act on.

httpx.ReadTimeout and several of its siblings stringify to the empty string, so
f"...: {e}" produced a message that stopped at the colon. The model saw no
reason for the failure and no hint about what to do instead, which is exactly
what it needed. These pin the invariant: never empty, and a timeout says so.
"""
import asyncio

import httpx
import pytest

from tool.mcp_tool import _describe_failure


EMPTY_STRINGIFYING = [
    httpx.ReadTimeout(""),
    httpx.ConnectTimeout(""),
    httpx.PoolTimeout(""),
    asyncio.TimeoutError(),
]


@pytest.mark.parametrize("exc", EMPTY_STRINGIFYING)
def test_exceptions_that_stringify_to_nothing_still_explain_themselves(exc):
    assert str(exc) == "", "test fixture assumes an empty message"
    text = _describe_failure(exc, "deepwiki", "read_wiki_contents")
    assert text.strip()
    assert "deepwiki" in text
    assert "read_wiki_contents" in text


@pytest.mark.parametrize("exc", EMPTY_STRINGIFYING)
def test_timeouts_are_named_as_timeouts(exc):
    text = _describe_failure(exc, "deepwiki", "read_wiki_contents")
    assert "timed out" in text.lower()
    # The model has to be told what to do differently, not just that it failed.
    assert "narrower" in text.lower() or "less" in text.lower()


def test_unreachable_server_points_at_the_connection():
    text = _describe_failure(httpx.ConnectError(""), "deepwiki", "ask_question")
    assert "deepwiki" in text
    assert "reach" in text.lower() or "disconnected" in text.lower()


def test_an_ordinary_error_keeps_its_own_message():
    text = _describe_failure(RuntimeError("boom"), "srv", "tool")
    assert "boom" in text


def test_an_error_with_no_message_falls_back_to_its_type():
    class WeirdFailure(Exception):
        pass

    text = _describe_failure(WeirdFailure(), "srv", "tool")
    assert "WeirdFailure" in text


def test_outer_budget_exceeds_the_container_default():
    """The layer that knows the server's limit has to be the one to give up.

    Equal budgets raced, and the outer one won with a message of "".
    """
    from sandbox.client import MCP_CALL_TIMEOUT_SECONDS

    container_default_per_server = 60
    assert MCP_CALL_TIMEOUT_SECONDS > container_default_per_server * 2
