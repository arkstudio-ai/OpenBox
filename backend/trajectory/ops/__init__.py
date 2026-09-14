"""Operations tooling for the trajectory deployment (SPEC §12).

Run as one-off commands, mostly inside the worker image by the scripts in
deploy/gw2/scripts:
- ``cms``: CloudMonitor custom metrics (host timer, alarm tests).
- ``backup``: presigned uploads and verification of PostgreSQL dumps.
- ``lifecycle``: merge of OSS lifecycle rules (standard library only, operator machine).
- ``rebuild``: trace database rebuild drill from archived segments.
"""
