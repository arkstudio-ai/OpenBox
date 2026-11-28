"""Count request tokens with the production vocabulary, whatever the machine has cached.

``agent.tool_payload.proxy_token_count`` uses the o200k vocabulary when it is on
disk (the image bakes it into ``TIKTOKEN_CACHE_DIR``) and otherwise a
conservative bytes/3 estimate (``core.local_tokenizer``). Context-budget tests
tuned to one count fail under the other, so their result depended on whether
the developer's temp directory happened to hold tiktoken's cache. litellm ships
the same checksum-verified vocabulary file; point the local loader at it. The
loader never downloads.
"""
from importlib import resources

import pytest


@pytest.fixture
def production_tokenizer(monkeypatch):
    from agent import tool_payload
    from core import local_tokenizer

    directory = resources.files("litellm").joinpath("litellm_core_utils/tokenizers")
    if not directory.joinpath(local_tokenizer.VOCABULARY_CACHE_KEY).is_file():
        pytest.skip("o200k vocabulary not available locally (litellm's bundled copy is missing)")
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(directory))
    encoding = tool_payload._proxy_encoding  # a test may replace the attribute; clear the real cache
    encoding.cache_clear()
    if encoding() is None:
        pytest.skip("o200k vocabulary in litellm does not match the pinned checksum")
    yield
    encoding.cache_clear()
