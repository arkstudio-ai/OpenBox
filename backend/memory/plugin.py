"""Long-term memory as kernel plugins (docs/KERNEL_DSH_COMPARISON_20261128.md 9.1, stage one).

The entry point registers ``MemoryPlugin`` into
``agent.extensions.KernelExtensions``. It registers nothing yet; the next
changes move memory's startup wiring, prompt sections, tools and recovery here.
"""
from __future__ import annotations

from core.log import create_logger

log = create_logger("memory.plugin")


class MemoryPlugin:
    """The long-term memory feature."""

    name = "memory"

    def register(self, registry) -> None:
        return None
