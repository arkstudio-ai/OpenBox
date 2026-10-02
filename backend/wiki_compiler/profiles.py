"""A closed declarative contract shared by profile validation and execution.

No expression, code, URL-template, provider instruction or imported gate runs.
Vocabulary is adapted from upstream CLP; unsupported declarations fail visibly.
"""
from copy import deepcopy
from datetime import date
import json
import math
import re


class ProfileError(ValueError):
    def __init__(self, code, path=""):
        self.code, self.path = code, path
        super().__init__(code)


def require(condition, path, code="wiki_profile_invalid"):
    if not condition:
        raise ProfileError(code, path)


def mapping(value, path, allowed=None):
    require(isinstance(value, dict), path)
    require(all(isinstance(key, str) for key in value), path)
    if allowed is not None:
        require(not set(value) - set(allowed), path, "wiki_profile_unsupported_field")
    return value


def identifier(value, path):
    require(isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", value), path)
    return value


def names(value, allowed, path):
    require(isinstance(value, list) and all(isinstance(item, str) and item in allowed for item in value), path)
    require(len(set(value)) == len(value), path)


FIELD_TYPES = {"string", "number", "integer", "boolean", "date", "slug", "enum", "string[]", "artifactRef", "artifactRef[]"}


def field_definitions(value, path):
    mapping(value, path)
    require(len(value) <= 40, path)
    for key, field in value.items():
        identifier(key, path)
        mapping(field, path + "." + key, {"type", "required", "default", "enum", "min", "max", "artifactTypes"})
        require(isinstance(field.get("type"), str) and field["type"] in FIELD_TYPES, path)
        require(isinstance(field.get("required", False), bool), path)
        if field["type"] == "enum":
            require(isinstance(field.get("enum"), list) and 0 < len(field["enum"]) <= 100
                and all(isinstance(item, str) and 0 < len(item) <= 160 for item in field["enum"]), path)
            require(len(set(field["enum"])) == len(field["enum"]), path)
        for bound in ("min", "max"):
            if bound in field:
                require(type(field[bound]) in {int, float} and math.isfinite(field[bound]), path)
        require(field.get("min", -math.inf) <= field.get("max", math.inf), path)
        if "default" in field:
            field_value(field["default"], field, path + "." + key)


def field_value(value, field, path):
    kind = field["type"]
    if kind in {"string", "date", "slug", "enum", "artifactRef"}:
        require(isinstance(value, str) and len(value) <= 8000, path, "wiki_record_field_invalid")
        if kind == "date":
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ProfileError("wiki_record_field_invalid", path) from exc
        elif kind == "slug":
            identifier(value, path)
        elif kind == "enum":
            require(value in field["enum"], path, "wiki_record_field_invalid")
        measure = len(value)
    elif kind in {"number", "integer"}:
        require(type(value) in ({int} if kind == "integer" else {int, float}) and math.isfinite(value), path, "wiki_record_field_invalid")
        measure = value
    elif kind == "boolean":
        require(type(value) is bool, path, "wiki_record_field_invalid")
        measure = 0
    else:
        require(isinstance(value, list) and len(value) <= 100 and all(isinstance(item, str) and len(item) <= 8000 for item in value), path,
                "wiki_record_field_invalid")
        measure = len(value)
    require(field.get("min", -math.inf) <= measure <= field.get("max", math.inf), path, "wiki_record_field_invalid")
    return value


def validate_fields(values, definitions, path="fields"):
    mapping(values, path, definitions)
    result = deepcopy(values)
    for key, field in definitions.items():
        if key not in result and "default" in field:
            result[key] = deepcopy(field["default"])
        require(key in result or not field.get("required"), path + "." + key, "wiki_record_required_field")
        if key in result:
            field_value(result[key], field, path + "." + key)
    return result


def _lifecycle(entity, path):
    lifecycle = entity.get("lifecycle")
    if "lifecycle" not in entity:
        return
    mapping(lifecycle, path, {"field", "initial", "terminal", "transitions", "transitionRequirements", "transitionRelationRequirements"})
    require(isinstance(lifecycle.get("field"), str), path)
    field = entity["fields"].get(lifecycle["field"], {})
    require(field.get("type") == "enum", path)
    states = field["enum"]
    require(lifecycle.get("initial") in states, path)
    names(lifecycle.get("terminal", []), states, path)
    mapping(lifecycle.get("transitions"), path, states)
    for start, targets in lifecycle["transitions"].items():
        names(targets, states, path)
        require(start not in lifecycle.get("terminal", []) or not targets, path)
    mapping(lifecycle.get("transitionRequirements", {}), path, states)
    for required in lifecycle.get("transitionRequirements", {}).values():
        names(required, entity["fields"], path)
    mapping(lifecycle.get("transitionRelationRequirements", {}), path, states)


def _stages(workflow, entities, relations, artifacts, path):
    stages = workflow.get("stages")
    require(isinstance(stages, list) and 1 <= len(stages) <= 30, path)
    seen = set()
    for stage in stages:
        mapping(stage, path, {"id", "title", "reads", "writes", "relationWrites", "artifactWrites", "gate", "gates",
                              "previousIds", "outputsRequired", "action"})
        key = identifier(stage.get("id"), path)
        require(isinstance(stage.get("title", ""), str) and len(stage.get("title", "")) <= 160, path)
        require(key not in seen, path)
        seen.add(key)
        for field, allowed in (("reads", entities), ("writes", entities), ("relationWrites", relations), ("artifactWrites", artifacts)):
            names(stage.get(field, []), allowed, path)
        stage.setdefault("reads", [])
        stage.setdefault("writes", [])
        required = stage.get("outputsRequired", 0)
        require(type(required) is int and 0 <= required <= 20, path)
        if "action" in stage:
            require(isinstance(stage["action"], str) and stage["action"] in {"organize", "compile"}, path)
        gates = stage.get("gates", [stage["gate"]] if "gate" in stage else [])
        require(isinstance(gates, list) and len(gates) <= 10, path)
        for gate in gates:
            require(isinstance(gate, str) and (re.fullmatch(r"human:[a-z0-9][a-z0-9_-]{0,79}", gate)
                    or gate in {"trust:sources-current", "agent:task-completed"}), path)
            require(gate != "agent:task-completed" or stage.get("action"), path)
        require(len(set(gates)) == len(gates), path)
        stage["gates"] = gates
        require(isinstance(stage.get("previousIds", []), list), path)
        for previous in stage.get("previousIds", []):
            identifier(previous, path)


def validate_profile(value):
    try:
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ProfileError("wiki_profile_invalid", "profile") from exc
    require(len(serialized) <= 200000, "profile", "wiki_profile_size_limit")
    profile = deepcopy(mapping(value, "profile", {"schemaVersion", "profileId", "profileVersion", "title", "entities", "relations", "artifacts", "workflows"}))
    require(type(profile.get("schemaVersion")) is int and profile["schemaVersion"] == 1, "schemaVersion")
    identifier(profile.get("profileId"), "profileId")
    require(isinstance(profile.get("profileVersion", ""), str) and len(profile.get("profileVersion", "")) <= 80, "profileVersion")
    require(isinstance(profile.get("title"), str) and 1 <= len(profile["title"]) <= 160, "title")
    for block in ("entities", "relations", "artifacts", "workflows"):
        profile.setdefault(block, {})
        mapping(profile[block], block)
        require(len(profile[block]) <= 40, block)
        for key in profile[block]:
            identifier(key, block)
    require(profile["entities"], "entities")
    for key, entity in profile["entities"].items():
        mapping(entity, key, {"title", "fields", "lifecycle"})
        require(isinstance(entity.get("title", ""), str) and len(entity.get("title", "")) <= 160, key)
        field_definitions(entity.get("fields", {}), key)
        entity.setdefault("fields", {})
        _lifecycle(entity, key)
    for key, relation in profile["relations"].items():
        mapping(relation, key, {"from", "to", "direction", "attributes", "requiredAttributes"})
        for role in ("from", "to"):
            names(relation.get(role), profile["entities"], key)
            require(relation[role], key)
        require(isinstance(relation.get("direction"), str) and relation["direction"] in {"directed", "symmetric"}, key)
        require(relation["direction"] != "symmetric" or set(relation["from"]) == set(relation["to"]), key)
        field_definitions(relation.get("attributes", {}), key)
        names(relation.get("requiredAttributes", []), relation.get("attributes", {}), key)
    for key, artifact in profile["artifacts"].items():
        mapping(artifact, key, {"title", "mediaTypes"})
        require(isinstance(artifact.get("title", ""), str) and len(artifact.get("title", "")) <= 160, key)
        names(artifact.get("mediaTypes"), {"text/plain", "text/markdown", "application/json"}, key)
        require(artifact["mediaTypes"], key)
    for key, workflow in profile["workflows"].items():
        mapping(workflow, key, {"title", "inputs", "stages"})
        require(isinstance(workflow.get("title", ""), str) and len(workflow.get("title", "")) <= 160, key)
        field_definitions(workflow.get("inputs", {}), key)
        _stages(workflow, profile["entities"], profile["relations"], profile["artifacts"], key)
    _validate_requirements(profile)
    return profile


def _validate_requirements(profile):
    for entity_key, entity in profile["entities"].items():
        for field in entity["fields"].values():
            if "artifactTypes" in field:
                require(field["type"] in {"artifactRef", "artifactRef[]"}, entity_key)
                names(field["artifactTypes"], profile["artifacts"], entity_key)
        for requirements in entity.get("lifecycle", {}).get("transitionRelationRequirements", {}).values():
            require(isinstance(requirements, list) and len(requirements) <= 20, entity_key)
            for requirement in requirements:
                mapping(requirement, entity_key, {"relationType", "role", "minCount", "otherTypes", "otherStates"})
                require(isinstance(requirement.get("relationType"), str), entity_key)
                relation = profile["relations"].get(requirement["relationType"])
                require(relation is not None and isinstance(requirement.get("role"), str) and requirement["role"] in {"from", "to"}, entity_key)
                role = requirement["role"]
                require(entity_key in relation[role], entity_key)
                require(type(requirement.get("minCount")) is int and 1 <= requirement["minCount"] <= 100, entity_key)
                other = relation["to" if role == "from" else "from"]
                names(requirement.get("otherTypes", other), other, entity_key)
                if "otherStates" in requirement:
                    allowed = set()
                    for kind in requirement.get("otherTypes", other):
                        definition = profile["entities"][kind]
                        lc = definition.get("lifecycle", {})
                        allowed.update(definition["fields"].get(lc.get("field"), {}).get("enum", []))
                    names(requirement["otherStates"], allowed, entity_key)


def validate_transition(entity, fields, target):
    lifecycle = entity.get("lifecycle")
    require(lifecycle is not None, "lifecycle", "wiki_record_no_lifecycle")
    current = fields.get(lifecycle["field"])
    require(target in lifecycle["transitions"].get(current, []), "transition", "wiki_record_transition_denied")
    for key in lifecycle.get("transitionRequirements", {}).get(target, []):
        require(key in fields and fields[key] not in (None, "", []), key, "wiki_record_required_field")
    return validate_fields({**fields, lifecycle["field"]: target}, entity["fields"])


def unreachable_states(entity):
    lifecycle = entity.get("lifecycle")
    if not lifecycle:
        return []
    visited, pending = set(), [lifecycle["initial"]]
    while pending:
        current = pending.pop()
        if current not in visited:
            visited.add(current)
            pending.extend(lifecycle["transitions"].get(current, []))
    return sorted(set(entity["fields"][lifecycle["field"]]["enum"]) - visited)
