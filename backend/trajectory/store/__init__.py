"""Dedicated trajectory trace database: engine, models, migrations, partitions.

Business modules never import this package. Only the trajectory worker (and
the embedded worker in desktop mode) opens the trace database.
"""
