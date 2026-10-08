"""Tool arguments for memory.search and memory.read.

V2 uses each read as it was returned; nothing is captured for replay.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant import memory
from assistant.knowledge_provenance import KnowledgeReference


class ScopeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    include_all_projects: bool = Field(default=False, strict=True,
        description="Explicitly include the actor's owned live projects and personal background; omit project_id.")

    @model_validator(mode="after")
    def one_selection(self):
        memory._filters(self.project_id, self.include_all_projects)
        return self


class SearchArgs(ScopeArgs):
    query: str = Field(min_length=1, max_length=500, description="Search confirmed memories and uploaded document text with existing BM25 and Qdrant retrieval.")
    limit: int = Field(default=10, ge=1, le=memory.MAX_ITEMS, strict=True)


class MemoryReference(KnowledgeReference):
    kind: Literal["memory", "source"]

    @model_validator(mode="before")
    @classmethod
    def exact_reference(cls, value):
        if not memory.valid_reference(value):
            raise ValueError("Use the complete source_ref returned by memory.search")
        return value


class ReadArgs(ScopeArgs):
    source_ref: MemoryReference = Field(description="Complete unchanged source_ref from memory.search.")
    source_id: str | None = Field(default=None, min_length=1, max_length=64,
        description="Omit to read the confirmed memory or uploaded text chunk. Select an exact sources[].id for its available original evidence; a complete chunk need not be the complete document.")
    max_chars: int = Field(default=8000, ge=1, le=memory.MAX_READ_CHARS, strict=True)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)
