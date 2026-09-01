# WUYING workspace namespacing

OpenBox sessions are grouped by project: two sessions in the same project use
the same working tree, while different projects do not. The canonical sandbox
layout is:

```text
/workspace/openbox/users/u-<stable hash>/
  projects/p-<stable hash>-<slug>/       # shared project working tree
  .openbox/
    uploads/p-<stable hash>/a-<stable hash>/<filename>
    snapshots/p-<stable hash>/           # git-dir outside the working tree
    exports/                              # user-visible Skill ZIP exports
    mcp-home/                             # tenant-local MCP process caches
    trash/

/data/skills/u-<stable hash>/             # installed Skill packages
/data/mcp/u-<stable hash>/config.json     # root-only MCP config/credentials
```

Raw user, project, and asset identifiers are never interpolated into a path.
The stable SHA-256-derived segments prevent path traversal and eliminate
same-slug collisions across users and projects. The slug suffix is only a
human-readable hint and is still validated as one safe path segment.

The legacy `project_directory(slug)` helper remains temporarily available for
migration and compatibility tests. Runtime code must resolve the owner and
project id and use the namespaced helpers instead. `/workspace/uploads` is
likewise compatibility-only; new attachments are delivered to their owning
tenant/project namespace.

Legacy `/workspace/<slug>` trees are not moved automatically: on a previously
shared desktop a slug alone cannot prove which user owns the directory. An
operator must inspect and migrate such data explicitly.

The same rule applies to pre-isolation entries directly under `/data/skills`
and the legacy `/data/mcp/config.json`: they are not projected into any scoped
catalogue automatically because the execution plane cannot prove an owner.

The backend sends only the `u-<stable hash>` segment to the Action Server in
`X-OpenBox-User-Scope`. WUYING services run with
`OPENBOX_REQUIRE_USER_SCOPE=1`, so Skill, MCP, and catalogue endpoints fail
closed when that header is absent or when an older server does not advertise
`tenant_catalogue_scopes_v1`. The legacy global `/workspace/skills` symlink is
removed in this mode because it would reveal every scoped package directory.

Skill and MCP installations are user-level rather than project-level: sessions
in the same account intentionally share them. Project files, attachments, and
snapshots remain project-scoped. Exported Skill archives are stored under the
user's internal workspace tree, so same-name exports from different users do
not overwrite each other.

## Security boundary

Namespacing prevents accidental collisions; it is not the production security
boundary. API ownership checks remain mandatory. A single shared WUYING
desktop is supported only for development and acceptance testing, where users
who obtain arbitrary desktop command execution could inspect other directories.
Production deployment keeps the stronger one-user-one-WUYING-desktop model.
