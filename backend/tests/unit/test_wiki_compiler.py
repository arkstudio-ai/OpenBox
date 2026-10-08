"""Standalone contract, provenance, cache and dependency-closure checks."""
import ast
from dataclasses import replace
from pathlib import Path
import sys

import pytest

from wiki_compiler import CompilePolicy, CompileRequest, SourceSnapshot, TargetSnapshot, WikiContractError, compile_candidate
from wiki_compiler.adapters import InMemoryCache
from wiki_compiler.contracts import Dependency
from wiki_compiler.hashing import cache_key, dependency_closure, source_changes, text_hash


class Model:
    def __init__(self, output=None):
        self.calls = 0
        self.output = output

    async def generate(self, request):
        self.calls += 1
        source = request.sources[0]
        return self.output or {"paragraphs": [{"text": "项目使用中文答复。", "citations": [
            {"source_id": source.id, "quote": source.text}]}]}, {"input_tokens": 12, "output_tokens": 8, "estimated_cost": None}


def request():
    text = "这个项目需要用中文回答。"
    source = SourceSnapshot("source-1", 1, text, text_hash(text), "opaque-domain", 7)
    return CompileRequest("project-agreement", "项目约定", "opaque-domain", (source,),
                          TargetSnapshot("page-1"), CompilePolicy("strong/model"))


def test_core_has_no_host_dependencies_and_only_exchange_uses_yaml():
    root = Path(__file__).parents[2] / "wiki_compiler"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [name.name for name in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            # OKF requires bounded YAML parsing; the compile runtime stays
            # standard-library-only and no core module may import host services.
            allowed = sys.stdlib_module_names | {"wiki_compiler"}
            if path.name == "exchange.py":
                allowed = allowed | {"yaml"}
            assert all(name.split(".", 1)[0] in allowed for name in modules), (path, modules)


@pytest.mark.asyncio
async def test_replaceable_adapter_and_unchanged_sources_reuse_without_model_io():
    model, cache = Model(), InMemoryCache()
    first = await compile_candidate(request(), model=model, cache=cache)
    second = await compile_candidate(request(), model=model, cache=cache)
    assert model.calls == 1
    assert first.candidate.candidate_hash == second.candidate.candidate_hash
    assert second.reused and second.usage["input_tokens"] == 0
    assert first.usage["estimated_cost"] is None
    changed = replace(request(), policy=CompilePolicy("strong/new-version"))
    await compile_candidate(changed, model=model, cache=cache)
    assert model.calls == 2
    assert cache_key(request()) != cache_key(changed)


@pytest.mark.asyncio
async def test_source_hash_mismatch_fails_before_cache_or_model():
    model = Model()
    invalid = replace(request(), sources=(replace(request().sources[0], text="被修改的正文"),))
    with pytest.raises(WikiContractError, match="invalid_source_snapshot"):
        await compile_candidate(invalid, model=model, cache=InMemoryCache())
    assert model.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("citation", [
    {"source_id": "other-user", "quote": "这个项目需要用中文回答。"},
    {"source_id": "source-1", "quote": "这个项目使用英文"},
    {"source_id": [], "quote": "这个项目需要用中文回答。"},
])
async def test_invalid_citation_is_never_a_candidate(citation):
    model = Model({"paragraphs": [{"text": "中文答复", "citations": [citation]}]})
    with pytest.raises(WikiContractError):
        await compile_candidate(request(), model=model)


def test_source_changes_include_acl_revision_and_reverse_closure():
    original = Dependency("source", "s1", 1, "hash", 7)
    changed = replace(original, acl_epoch=8)
    assert source_changes((original,), (changed,))["changed"] == ("s1",)
    pages, sources = dependency_closure({"s1"}, {"p1": ("s1", "s2"), "p2": ("s2", "s3"), "unrelated": ("s4",)})
    assert pages == ("p1", "p2")
    assert sources == ("s1", "s2", "s3")
