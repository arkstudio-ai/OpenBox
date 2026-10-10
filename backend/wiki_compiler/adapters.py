"""Replaceable side-effect boundaries; no host, environment or filesystem IO."""
from typing import Protocol

from wiki_compiler.contracts import CompileRequest


class WikiModel(Protocol):
    async def generate(self, request: CompileRequest) -> tuple[dict, dict]: ...


class CompilationCache(Protocol):
    async def get(self, key: str) -> dict | None: ...
    async def put(self, key: str, value: dict) -> None: ...


class InMemoryCache:
    def __init__(self):
        self._values: dict[str, dict] = {}

    async def get(self, key: str) -> dict | None:
        import copy
        return copy.deepcopy(self._values.get(key))

    async def put(self, key: str, value: dict) -> None:
        import copy
        self._values[key] = copy.deepcopy(value)
