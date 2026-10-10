# Source-grounded Wiki candidate compiler

This is a standalone Python rewrite of the hash, dependency closure and review
candidate patterns inspected in `atomicstrata/llm-wiki-compiler`, fixed commit
`eb18e769db8096c80b81665fb7195e3b29f4cb6b` (MIT, 2026 atomicmemory).
No upstream runtime dependency is installed and no TypeScript is copied.
The upstream MIT notice is retained in `UPSTREAM_LICENSE` for attribution.

| Reference source | Local implementation | Host responsibility |
| --- | --- | --- |
| `src/compiler/hasher.ts` | `hashing.source_changes` | Supply current immutable source snapshots |
| `src/compiler/deps.ts` | `hashing.dependency_closure` | Persist reverse dependencies and invalidate SQL pages |
| `src/compiler/candidates.ts` | Immutable `CandidateDraft`, mandatory full-envelope hash | SQL revision/hash/target/source/ACL CAS approval |
| `src/sdk/core-types.ts` | Versioned dataclasses and replaceable model/cache protocols | Authentication, budgets, jobs, storage, publication, index outbox |
| `src/compiler/extraction-phase.ts`, `extraction-merge.ts`, `refresh-plan.ts` | `organization.validate_concepts`, `reconcile_concepts` | Scoped identity catalogue, persistent cache, incremental jobs, manual corrections and budgeted maintenance |
| `src/export/okf/*`, `src/import/okf-read.ts`, `okf-map.ts` | `exchange`, `export` | Reviewed imports, current-source checks, attachment storage and authenticated downloads |
| `src/export/{json-export,json-ld,graphml,marp,llms-txt}.ts` | `export.render_export` | A common freshly authorized SQL snapshot |
| `src/profile/*`, `src/trust/lifecycle-*`, `src/workflow-history/*` | `profiles`, `profile_templates` | Typed records, CAS transitions, exact-output approvals, atomic batches, durable tasks, adaptation and audit history |

The compilation and organization core imports only Python standard-library
modules and this package. The optional OKF codec in `exchange.py` uses the host's
existing PyYAML dependency for bounded YAML frontmatter parsing; other core
modules do not import third-party or host modules. This boundary is tested.
It does not read credentials, connect to SQL/Qdrant, infer authorization from
opaque domain IDs, publish pages or schedule work. Every paragraph requires an
exact source quote; citation validation proves provenance, not factual truth.
The host must recheck source content hashes, memory admission, current ACL and
target versions before every external data boundary and before publication.

The authenticated reader and source picker live in `memory/wiki/reader.py`;
the host UI lives in `frontend-v2/src/features/memory/wiki/`. The reader borrows
the upstream viewer's page, citation and provenance patterns while retaining
OpenBox's live authorization and invalidation checks. Prompt/output changes are
versioned by `CompilePolicy.prompt_version` (`markdown-paragraph-citations-v3`).
See `docs/WIKI_READER_IMPLEMENTATION.md` for the reader and
`docs/WIKI_PLATFORM_IMPLEMENTATION.md` for organization, exchange, profiles,
workflows, upstream interoperability and the completed acceptance matrix.

Profiles use a closed local declaration contract. Imported profiles and workflow
history remain inert metadata. They cannot install commands, providers, gates or
executable authority. The retained internal platform supports source organization,
compilation and human-review stages. Consumer publication instead uses the host's
automatic grounding policy: independently verify every claim against full evidence,
or preserve admitted memory statements; bind the proof to the immutable draft hash
and recheck all source, ACL and target versions before atomic publication. Model
output alone never grants publication authority. The consumer UI exposes reading
and simple edits, with background maintenance and shared bounded call budgets.
See `docs/CONSUMER_KNOWLEDGE_IMPLEMENTATION.md` for current behavior and evidence.
