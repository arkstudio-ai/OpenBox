"""Trajectory worker: spool ingest, projection, archive, retention and the admin API.

The worker is the single writer of the trace database (SPEC §8). It runs as
its own process (``python -m trajectory.worker``) in server deployments and as
asyncio tasks inside the backend process on desktops (embedded mode). Modules
are imported on demand; importing this package starts nothing.
"""
