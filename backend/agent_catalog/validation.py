"""The same definition compilation boundary for forms and chat proposals."""
from agent_catalog.compiler import compile_agent
from skill.snapshot import freeze_specs
from team.errors import TeamError


async def agent_summary(spec, actor, *, strict=False, sandbox=None) -> dict:
    from core.config import get_config
    try:
        skills = await freeze_specs([spec], actor, sandbox=sandbox)
        compiled = compile_agent(spec, config=get_config(), role="trial", skills=skills)
        return {**compiled.summary, "_compiled_trial": compiled.snapshot()}
    except TeamError as exc:
        if strict:
            raise
        return {"issues": [exc.to_dict()]}
