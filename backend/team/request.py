"""Carry a user's team selection through Inbox's existing JSON input options."""
from agent_catalog.schemas import TeamRequest

_ENVELOPE = "$openbox-team-input-v1"


def pack(output_format: dict | None, team_request: dict | None, agent: str | None) -> dict | None:
    if output_format and _ENVELOPE in output_format:
        raise ValueError("output format uses a platform-reserved envelope")
    if team_request is None:
        return output_format
    if agent != "team":
        raise ValueError("team_request requires the team Agent mode")
    request = TeamRequest.model_validate(team_request).model_dump(mode="json")
    # This JSON column already holds per-input options. The envelope is private
    # to Inbox: the original format and typed selection become separate fields
    # on the claimed Message/TextPart, without adding a second schema column.
    return {_ENVELOPE: 1, "format": output_format, "team_request": request}


def unpack(value: dict | None) -> tuple[dict | None, dict | None]:
    if value and value.get(_ENVELOPE) == 1:
        return value.get("format"), TeamRequest.model_validate(value["team_request"]).model_dump(mode="json")
    return value, None
