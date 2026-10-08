"""Strict public schedule arguments, independent of actor/workspace authority."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class At(Strict):
    kind: Literal["at"]
    at: str = Field(min_length=1, max_length=128)


class Every(Strict):
    kind: Literal["every"]
    every_ms: int = Field(gt=0, le=2**53, strict=True)
    anchor_ms: int | None = Field(default=None, ge=0, le=2**53, strict=True)


class Cron(Strict):
    kind: Literal["cron"]
    expr: str = Field(min_length=1, max_length=256)
    tz: str = Field(default="UTC", min_length=1, max_length=128)


Schedule = Annotated[At | Every | Cron, Field(discriminator="kind")]


class CreateFields(Strict):
    project_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    instructions: str = Field(min_length=1, max_length=8000)
    schedule: Schedule
    enabled: bool = Field(default=True, strict=True)


class Patch(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    instructions: str | None = Field(default=None, min_length=1, max_length=8000)
    schedule: Schedule | None = None
    enabled: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("Provide at least one non-null schedule change")
        return self
