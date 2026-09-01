# Archived Kubernetes sandbox manifests

These manifests are retained only as migration history. They are **not a
supported OpenBox deployment path** and must not be applied to the current
backend: the runtime accepts `SANDBOX_PROVIDER=wuying` only and deliberately
fails closed for Docker, Kubernetes and unknown execution providers.

Use Docker Compose for PostgreSQL/Redis/Azurite dependencies and deploy the
Agent execution plane through the WUYING workflow in
[`docs/WUYING_SANDBOX.md`](../docs/WUYING_SANDBOX.md).
