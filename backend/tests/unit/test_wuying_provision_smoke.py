from argparse import Namespace

from core.config import OpenBoxConfig
from scripts import wuying_provision_smoke as smoke


def _args(**overrides):
    values = {
        "allow_prepaid": False,
        "keep": False,
    }
    values.update(overrides)
    return Namespace(**values)


async def test_full_prepaid_guard_stops_before_provision(monkeypatch):
    import core.config as config_module

    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: OpenBoxConfig(wuying_charge_type="PrePaid"),
    )
    assert await smoke.tier_full(_args()) == 2


async def test_channel_prepaid_requires_keep(monkeypatch):
    import core.config as config_module

    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: OpenBoxConfig(wuying_charge_type="PrePaid"),
    )
    assert await smoke.tier_channel(_args(allow_prepaid=True)) == 2
