"""The dev server talks to its providers directly, not through a VPN proxy."""
import importlib.util
import os
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "backend_entrypoint", BACKEND_DIR / "scripts" / "backend_entrypoint.py"
)
entrypoint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(entrypoint)


@pytest.fixture
def clean_env(monkeypatch):
    for name in entrypoint._PROXY_VARS + ("OPENBOX_KEEP_PROXY",):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_an_inherited_proxy_is_dropped(clean_env):
    """A globally-exported VPN proxy silently reroutes every provider call.

    The relay, OSS and DashScope are all mainland-direct; routing them through
    a laptop's tunnel turns a healthy provider into "ConnectError" and strands
    generations that were already paid for.
    """
    clean_env.setenv("HTTPS_PROXY", "http://127.0.0.1:10808")
    clean_env.setenv("http_proxy", "http://127.0.0.1:10808")

    entrypoint._drop_inherited_proxy()

    assert "HTTPS_PROXY" not in os.environ
    assert "http_proxy" not in os.environ


def test_no_proxy_is_left_alone(clean_env):
    """NO_PROXY is an exemption list, not a proxy — dropping it does nothing good."""
    clean_env.setenv("NO_PROXY", "localhost,127.0.0.1")

    entrypoint._drop_inherited_proxy()

    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1"


def test_a_deployment_that_needs_one_can_keep_it(clean_env):
    clean_env.setenv("HTTPS_PROXY", "http://corp-egress:3128")
    clean_env.setenv("OPENBOX_KEEP_PROXY", "1")

    entrypoint._drop_inherited_proxy()

    assert os.environ["HTTPS_PROXY"] == "http://corp-egress:3128"
