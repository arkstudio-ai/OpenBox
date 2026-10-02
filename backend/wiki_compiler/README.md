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

The compiler imports only Python standard-library modules and this package.
It does not read credentials, connect to SQL/Qdrant, infer authorization from
opaque domain IDs, publish pages or schedule work. Every paragraph requires an
exact source quote; citation validation proves provenance, not factual truth.
The host must recheck source content hashes, memory admission, current ACL and
target versions before every external data boundary and before publication.
