#!/usr/bin/env python3
"""Repository CLI. The standalone implementation lives in backend/sandbox/obx_diag.py;
the backend ships that file to desktops inline and with the browser runtime."""
from pathlib import Path
import runpy

runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "backend/sandbox/obx_diag.py"),
    run_name="__main__",
)
