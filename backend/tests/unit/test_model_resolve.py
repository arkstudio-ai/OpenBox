"""A stored model name must not outlive the provider that served it.

Sessions persist the model they started with. Swapping the provider gateway
leaves every old conversation carrying a name the new gateway rejects — as an
opaque "no channel available" the retry layer attempts five times before the
turn fails. These pin the rule that keeps those conversations usable.
"""
from agent.model_resolve import configured_models, is_available, resolve


class Cfg:
    def __init__(self, model, models):
        self.model = model
        self.models = [type("M", (), {"id": m})() for m in models]


CONFIGURED = Cfg("openai/gpt-5.6-luna", ["openai/gpt-5.6-luna", "openai/claude-opus-5"])
#: A deployment that never enumerated its models.
OPEN = Cfg("openai/gpt-5.6-luna", [])


def test_a_model_the_deployment_still_offers_is_kept():
    assert resolve("openai/claude-opus-5", CONFIGURED) == ("openai/claude-opus-5", None)


def test_a_retired_model_falls_back_and_reports_what_it_replaced():
    model, replaced = resolve("openai/gemini-3.7-flash", CONFIGURED)
    assert model == "openai/gpt-5.6-luna"
    assert replaced == "openai/gemini-3.7-flash", "the caller has to be able to tell the user"


def test_no_request_means_the_default():
    assert resolve(None, CONFIGURED) == ("openai/gpt-5.6-luna", None)


def test_an_unenumerated_deployment_rules_nothing_out():
    """Without a models list nothing is known to be missing, so a name that
    might be valid must not be replaced by guesswork."""
    assert resolve("anything/at-all", OPEN) == ("anything/at-all", None)
    assert is_available("anything/at-all", OPEN)


def test_the_default_is_offered_even_when_absent_from_the_list():
    cfg = Cfg("openai/gpt-5.6-luna", ["openai/claude-opus-5"])
    assert "openai/gpt-5.6-luna" in configured_models(cfg)
    assert resolve("openai/gpt-5.6-luna", cfg) == ("openai/gpt-5.6-luna", None)


def test_removed_gpt_preference_cannot_reenter_via_voice_or_old_messages():
    from agent.model_resolve import resolve_step_model
    config = Cfg("openai/qwen3.8-flash", ["openai/qwen3.8-flash"])
    for field in ("agent_model", "message_model", "session_model"):
        values = dict(agent_model=None, message_model=None, session_model=None)
        values[field] = "openai/gpt-5.6-luna"
        selection = resolve_step_model(**values, config=config)
        assert selection.model_id == config.model
        assert selection.replaced_from == "openai/gpt-5.6-luna"
