---
name: skill-creator
description: Create a user's personal OpenBox Skill through natural conversation, including its SKILL.md and optional scripts, references, or assets; export it as a ZIP when requested.
allowed-tools:
  - skill_manage
---

# OpenBox Skill Creator

Create a reusable personal Skill from what the user wants the agent to do. The
full authoring guidance and the `skill_manage` schema are loaded only for this
agent run; do not copy this Skill into the core prompt.

## Conversation

Understand the recurring job, the requests that should trigger it, the desired
outcome, and any real constraints. Ask only for information that materially
changes the Skill; infer a concise name and sensible workflow when the user has
already been clear. Preserve the user's chosen product, scope, permissions, and
external-action boundaries.

Before creating, briefly state the proposed Skill name and what it will do. If
the user has directly asked you to create it and the requirements are clear,
continue without a redundant confirmation.

## Package contract

Every package has a root `SKILL.md`:

```text
skill-name/
|-- SKILL.md
|-- scripts/       optional deterministic helpers
|-- references/    optional detail loaded only when relevant
|-- assets/        optional output templates or static inputs
`-- agents/        optional UI metadata
```

Use a lowercase name of at most 64 characters containing only letters, digits,
and hyphens. The YAML frontmatter must contain the same `name` and a concise,
discriminating `description` that says what the Skill does and when it applies.
OpenBox also supports `icon`, `requires-mcp`, `homepage`, and `allowed-tools`
when they are genuinely needed.

Keep `SKILL.md` focused on purpose, essential workflow, non-obvious constraints,
and routing to resources. Put substantial conditional guidance in
`references/`; put repeatable deterministic work in `scripts/`; put files meant
for generated output in `assets/`. Do not add README, changelog, generic advice,
empty directories, examples, or placeholders without a concrete use.

Never put API keys, passwords, cookies, environment files, or other credentials
inside a Skill. A Skill may describe which configured capability it needs, but
must not copy secrets from configuration or chat into its files.

## Create and validate

Call `skill_manage(action="create")` once with the complete `SKILL.md` and any
needed UTF-8 text resources. Do not bypass it with `write`, `bash`, or a pasted
install request: the tool validates paths, writes atomically, records ownership,
and creates the private downloadable snapshot.

Creation installs the Skill into this user's persistent skill directory. It
appears under **Skill Centre → Mine → Personal** as **Not uploaded** and is ready
for the agent to discover on the next run. Publishing is deliberately separate:
the user must explicitly choose Publish in Skill Centre before everybody can see
and install the snapshot from the store.

After creation, report the name and a short usage example. Do not claim it is
public until the publish action has succeeded.

## ZIP handoff

When the user asks to download or receive the Skill package, call
`skill_manage(action="export", name="...")`, then immediately call `share_file`
with the exact returned ZIP path. Do not rebuild the archive with shell commands.
