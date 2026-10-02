"""Reviewable definition drift; completed stages cannot silently be rewritten."""
from copy import deepcopy

from db.base import get_db_session
from memory.wiki.workflows import _find, preflight_outputs, stage_state, stages_for
from wiki_compiler.hashing import canonical_hash
from wiki_compiler.profiles import require, validate_fields


def plan(run, profile):
    new_workflow = profile.definition["workflows"].get(run.workflow_id)
    old = stages_for(run)
    if new_workflow is None:
        return {"allowed": False, "reason": "workflow_removed", "new_hash": profile.definition_hash}
    new = new_workflow["stages"]
    mapping, used, issues = {}, set(), []
    for previous in old:
        matches = [stage for stage in new if stage["id"] == previous["id"] or previous["id"] in stage.get("previousIds", [])]
        if len(matches) != 1 or matches[0]["id"] in used:
            issues.append("stage_mapping_ambiguous:" + previous["id"])
            continue
        mapping[previous["id"]] = matches[0]["id"]
        used.add(matches[0]["id"])
    for index, previous in enumerate(old[:run.stage_index]):
        expected = mapping.get(previous["id"])
        if index >= len(new) or new[index]["id"] != expected:
            issues.append("completed_stage_order_changed:" + previous["id"])
            continue
        strip = lambda stage: {key: value for key, value in stage.items() if key not in {"id", "previousIds", "title"}}
        if strip(previous) != strip(new[index]):
            issues.append("completed_stage_definition_changed:" + previous["id"])
    current = mapping.get(old[run.stage_index]["id"])
    if run.stage_index >= len(new) or new[run.stage_index]["id"] != current:
        issues.append("current_stage_order_changed")
    details = {"allowed": not issues, "issues": issues, "mapping": mapping, "old_hash": run.profile_hash,
               "new_hash": profile.definition_hash, "new_definition": profile.definition,
               "revision": run.revision, "approvals_will_reset": True}
    details["preview_hash"] = canonical_hash(details)
    return details


async def preview(*, user_id, workspace_id, run_id):
    async with get_db_session() as db:
        run, _, profile = await _find(db, user_id, workspace_id, run_id)
        if run is None:
            return None
        require(run.status not in {"COMPLETED", "CANCELLED"} and profile is not None, "run", "wiki_workflow_terminal")
        return plan(run, profile)


async def apply_adaptation(db, scope, run, profile, payload):
    change = plan(run, profile)
    require(change["allowed"], "adaptation", "wiki_workflow_adaptation_blocked")
    require(payload.get("preview_hash") == change["preview_hash"], "preview", "wiki_workflow_adaptation_changed")
    inputs = validate_fields(run.inputs, profile.definition["workflows"][run.workflow_id].get("inputs", {}), "inputs")
    states = {stage["id"]: stage_state() for stage in profile.definition["workflows"][run.workflow_id]["stages"]}
    for old_id, new_id in change["mapping"].items():
        state = deepcopy(run.stages[old_id])
        state["approvals"] = {}
        states[new_id] = state
    run.definition, run.profile_hash, run.inputs = profile.definition, profile.definition_hash, inputs
    stage = stages_for(run)[run.stage_index]
    await preflight_outputs(db, scope, run, stage, states[stage["id"]]["outputs"])
    run.stages = states
    return {"old_hash": change["old_hash"], "new_hash": change["new_hash"], "mapping": change["mapping"]}
