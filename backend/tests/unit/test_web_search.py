"""Tests for built-in web search provider selection and Tavily requests."""

from types import SimpleNamespace

import httpx
import pytest

from tool.tool import ToolContext, ToolResult
from tool import web_search


@pytest.mark.asyncio
async def test_execute_uses_tavily_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")

    import core.config

    monkeypatch.setattr(core.config, "get_config", lambda: SimpleNamespace(provider={}))

    async def fake_tavily(args: web_search.WebSearchArgs, api_key: str) -> ToolResult:
        assert args.query == "OpenBox"
        assert api_key == "tvly-test-key"
        return ToolResult(title="Search: OpenBox", output="ok", metadata={"provider": "tavily"})

    async def fail_duckduckgo(query: str, max_results: int) -> ToolResult:
        pytest.fail(f"unexpected DuckDuckGo fallback for {query!r} ({max_results})")

    monkeypatch.setattr(web_search, "_search_tavily", fake_tavily)
    monkeypatch.setattr(web_search, "_search_duckduckgo", fail_duckduckgo)

    result = await web_search.execute(web_search.WebSearchArgs(query="OpenBox"), ToolContext())

    assert result.metadata["provider"] == "tavily"


@pytest.mark.asyncio
async def test_tavily_request_and_result_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "answer": "OpenBox is an AI Agent execution platform.",
                "results": [
                    {
                        "title": "OpenBox",
                        "url": "https://example.com/openbox",
                        "content": "An isolated Agent runtime.",
                        "score": 0.99,
                    }
                ],
            }

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    args = web_search.WebSearchArgs(
        query="OpenBox",
        max_results=3,
        search_depth="advanced",
        topic="general",
        time_range="week",
        include_answer=True,
    )
    result = await web_search._search_tavily(args, "tvly-test-key")

    assert captured["url"] == "https://api.tavily.com/search"
    assert captured["headers"] == {
        "Content-Type": "application/json",
        "Authorization": "Bearer tvly-test-key",
    }
    assert captured["json"] == {
        "query": "OpenBox",
        "max_results": 3,
        "search_depth": "advanced",
        "topic": "general",
        "include_answer": True,
        "include_raw_content": False,
        "include_images": False,
        "time_range": "week",
    }
    assert result.metadata["provider"] == "tavily"
    assert result.metadata["results"][0]["url"] == "https://example.com/openbox"
