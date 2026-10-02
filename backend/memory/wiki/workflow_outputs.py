"""Closed output vocabulary; stages cannot write undeclared types or targets."""
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from typing import Annotated, Literal, Union

from memory.wiki import records
from wiki_compiler.profiles import ProfileError, require


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PageOutput(Strict):
    kind: Literal["page"]
    entity_type: str
    slug: str
    title: str = Field(min_length=1, max_length=160)
    fields: dict
    page_id: str
    page_revision: int = Field(ge=1)
    expected_revision: int = Field(ge=0)


class LifecycleOutput(Strict):
    kind: Literal["lifecycle"]
    record_id: str
    expected_revision: int = Field(ge=1)
    to: str


class RelationOutput(Strict):
    kind: Literal["relation"]
    type: str
    from_id: str
    from_revision: int = Field(ge=1)
    to_id: str
    to_revision: int = Field(ge=1)
    attributes: dict = Field(default_factory=dict)
    expected_revision: int = Field(ge=0)


class RecordReference(Strict):
    id: str
    revision: int = Field(ge=1)


class ArtifactOutput(Strict):
    kind: Literal["artifact"]
    type: str
    name: str = Field(min_length=1, max_length=160)
    media_type: Literal["text/plain", "text/markdown", "application/json"]
    body: str = Field(min_length=1, max_length=64000)
    records: list[RecordReference] = Field(min_length=1, max_length=20)


Output = Annotated[Union[PageOutput, LifecycleOutput, RelationOutput, ArtifactOutput], Field(discriminator="kind")]
OUTPUTS = TypeAdapter(list[Output])


def validate_outputs(value):
    try:
        require(isinstance(value, list) and len(value) <= 20, "outputs", "wiki_workflow_output_limit")
        return [item.model_dump() for item in OUTPUTS.validate_python(value)]
    except ValidationError as exc:
        raise ProfileError("wiki_workflow_output_invalid") from exc


async def apply_output(db, scope, profile, stage, output, *, apply=False, run_id=None):
    output = validate_outputs([output])[0]
    kind = output["kind"]
    if kind == "page":
        require(output["entity_type"] in stage.get("writes", []), "writes", "wiki_workflow_write_denied")
        return await records.save_record(db, scope, profile, output, apply=apply)
    if kind == "lifecycle":
        record = await records.get_record(db, scope, profile, output["record_id"], revision=output["expected_revision"])
        require(record.entity_type in stage.get("writes", []), "writes", "wiki_workflow_write_denied")
        return await records.transition(db, scope, profile, output, apply=apply)
    if kind == "relation":
        require(output["type"] in stage.get("relationWrites", []), "relationWrites", "wiki_workflow_write_denied")
        for role in ("from", "to"):
            record = await records.get_record(db, scope, profile, output[role + "_id"])
            require(record.entity_type in stage.get("reads", []) + stage.get("writes", []), "reads", "wiki_workflow_read_denied")
        return await records.save_relation(db, scope, profile, output, apply=apply)
    require(output["type"] in stage.get("artifactWrites", []), "artifactWrites", "wiki_workflow_write_denied")
    for reference in output["records"]:
        record = await records.get_record(db, scope, profile, reference["id"])
        require(record.entity_type in stage.get("reads", []) + stage.get("writes", []), "reads", "wiki_workflow_read_denied")
    return await records.save_artifact(db, scope, profile, output, apply=apply, run_id=run_id)


async def output_visible(db, scope, profile, output):
    """Read projection checks provenance without replaying an old write version."""
    try:
        if output["kind"] == "page":
            await records.current_page(db, scope, output["page_id"], output["page_revision"])
        else:
            ids = ([output["record_id"]] if output["kind"] == "lifecycle" else
                   [output["from_id"], output["to_id"]] if output["kind"] == "relation" else
                   [item["id"] for item in output["records"]])
            for record_id in ids:
                row = await records.get_record(db, scope, profile, record_id)
                if not await records.current_record(db, scope, profile, row):
                    return False
        return True
    except (ProfileError, records.service.WikiStateError):
        return False
