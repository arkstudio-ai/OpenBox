"""The token proxy never fetches tiktoken's vocabulary on the event loop."""
from agent import tool_payload


def test_encoding_is_skipped_without_a_populated_cache(monkeypatch, tmp_path):
    tool_payload._proxy_encoding.cache_clear()
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    monkeypatch.delenv("DATA_GYM_CACHE_DIR", raising=False)
    assert tool_payload._proxy_encoding() is None
    tool_payload._proxy_encoding.cache_clear()
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    assert tool_payload._proxy_encoding() is None
    # The estimate still answers.
    assert tool_payload.proxy_token_count("hello world") > 0
    tool_payload._proxy_encoding.cache_clear()


def test_encoding_loads_from_a_populated_cache(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace

    (tmp_path / "vocab").write_bytes(b"x")
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    calls = []
    fake = SimpleNamespace(get_encoding=lambda name: calls.append(name) or SimpleNamespace(
        encode=lambda text, disallowed_special=(): list(text.split())))
    monkeypatch.setitem(sys.modules, "tiktoken", fake)
    tool_payload._proxy_encoding.cache_clear()
    assert tool_payload.proxy_token_count("a b c") == 3
    assert calls == ["o200k_base"]
    tool_payload._proxy_encoding.cache_clear()
