"""Operations tooling for the trajectory deployment (SPEC §12).

Run as one-off commands, mostly inside the worker image by the scripts in
deploy/gw2/scripts:
- ``cms``: CloudMonitor custom metrics (host timer, alarm tests).
- ``backup``: presigned uploads, verification and downloads of PostgreSQL dumps.
- ``lifecycle``: merge of OSS lifecycle rules (standard library only, operator machine).
- ``rebuild``: trace database rebuild drill from archived segments.
- ``deletion``: checks of the session deletion drill (tombstone, GC queue, objects under the prefix).
- ``latency``: recording off/on comparison of request p95 and backend CPU for the release.
"""
