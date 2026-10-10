"""Tool arguments for knowledge.directory and knowledge.read.

V2 uses each read as it was returned; nothing is captured for replay.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant import knowledge


class DirectoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    include_all_projects: bool = Field(default=False, strict=True,
        description="Explicitly search the actor's owned live projects as well as personal background. Omit project_id.")
    query: str = Field(default="", max_length=200, description="Literal title search only, not document contents.")
    limit: int = Field(default=20, ge=1, le=50, strict=True)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def one_selection(self):
        if self.project_id is not None and self.include_all_projects:
            raise ValueError("Select one project or explicitly select all owned projects")
        return self


class KnowledgeReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    kind: Literal["wiki"]
    id: str = Field(min_length=1, max_length=64)
    assistant_session_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    workspace_id: str = Field(min_length=1, max_length=64)
    project_id: str | None = Field(min_length=1, max_length=64)
    visibility: Literal["PERSONAL"]
    revision: int = Field(ge=1, le=0x7FFFFFFF)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependencies_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    acl_epoch: int = Field(ge=1, le=0x7FFFFFFF)

    @model_validator(mode="before")
    @classmethod
    def exact_reference(cls, value):
        if not knowledge._valid_reference(value):
            raise ValueError("Use the exact source_ref returned by knowledge.directory")
        return value


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_ref: KnowledgeReference = Field(description="Complete source_ref from knowledge.directory, unchanged.")
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    include_all_projects: bool = Field(default=False, strict=True,
        description="Explicitly allow the actor's owned live projects as well as personal background. Omit project_id.")
    max_chars: int = Field(default=8000, ge=1, le=knowledge.MAX_READ_CHARS, strict=True)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def one_selection(self):
        if self.project_id is not None and self.include_all_projects:
            raise ValueError("Select one project or explicitly select all owned projects")
        return self
