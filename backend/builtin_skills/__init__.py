"""Builtin skill packages shipped inside the backend image.

Each package owns its SKILL.md (model-facing), skill.yaml (platform contract)
and handlers (registered into skill_runtime.registry's static allowlist).
Platform core never imports domain modules from here — the registry and the
manifest catalog are the only seams.
"""
