"""Trajectory recording and run timing as a kernel plugin.

``TrajectoryPlugin`` registers the implementations of the kernel's ports
(``agent.extensions``). Recording itself is unchanged: business code only
enqueues facts (fail-open), the trajectory worker persists them
(docs/trajectory-rearch/SPEC.md).
"""
from __future__ import annotations


class TrajectoryPlugin:
    name = "trajectory"

    def register(self, registry) -> None:
        from trajectory.latency import TrajectoryTiming
        registry.set_timing(TrajectoryTiming())
