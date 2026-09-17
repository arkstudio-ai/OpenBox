"""Permission decisions may become stricter, never looser, across layers."""

import asyncio
from types import SimpleNamespace

import pytest

import permission.permission as permission_mod
from agent.hooks import ToolHooks
from agent.loop import _get_permission_rules, _get_platform_guard_rules
from permission.permission import PermissionDeniedError, Rule
from tool.batch import BatchArgs, Invocation, execute as execute_batch
from tool.tool import ToolContext, ToolResult


@pytest.fixture(autouse=True)
def _isolated_permission_state(monkeypatch):
    permission_mod._approved.clear()
    permission_mod._loaded_users.clear()
    permission_mod._pending.clear()
    monkeypatch.setattr(permission_mod, "_get_redis_client", lambda: None)
    yield
    permission_mod._approved.clear()
    permission_mod._loaded_users.clear()
    permission_mod._pending.clear()


@pytest.mark.asyncio
async def test_persisted_allow_cannot_override_current_trusted_deny():
    permission_mod._approved["user-a"] = [
        Rule(permission="bash", pattern="*", action="allow"),
    ]

    with pytest.raises(PermissionDeniedError):
        await permission_mod.ask(
            session_id="session-a",
            permission="bash",
            patterns=["rm protected.txt"],
            config_rules=[Rule(permission="bash", pattern="*", action="deny")],
            user_id="user-a",
        )


@pytest.mark.asyncio
async def test_persisted_allow_can_resolve_a_current_trusted_ask(monkeypatch):
    permission_mod._approved["user-a"] = [
        Rule(permission="bash", pattern="*", action="allow"),
    ]
    published = []
    monkeypatch.setattr(permission_mod.bus, "publish", lambda *args: published.append(args))

    await permission_mod.ask(
        session_id="session-a",
        permission="bash",
        patterns=["echo safe"],
        config_rules=[Rule(permission="bash", pattern="*", action="ask")],
        user_id="user-a",
    )

    assert published == []
    assert permission_mod._pending == {}


@pytest.mark.asyncio
async def test_all_targets_are_preflighted_before_an_ask_is_published(monkeypatch):
    published = []
    monkeypatch.setattr(permission_mod.bus, "publish", lambda *args: published.append(args))

    with pytest.raises(PermissionDeniedError) as error:
        await permission_mod.ask(
            session_id="session-a",
            permission="read",
            patterns=["needs-confirmation.txt", "blocked.txt"],
            config_rules=[
                Rule(permission="read", pattern="needs-confirmation.txt", action="ask"),
                Rule(permission="read", pattern="blocked.txt", action="deny"),
            ],
            user_id="user-a",
        )

    assert error.value.pattern == "blocked.txt"
    assert published == []
    assert permission_mod._pending == {}


@pytest.mark.asyncio
async def test_platform_guard_cannot_be_relaxed_by_agent_or_user_allow():
    permission_mod._approved["user-a"] = [
        Rule(permission="read", pattern="*", action="allow"),
    ]
    hooks = ToolHooks(
        session_id="session-a",
        user_id="user-a",
        config_rules=[Rule(permission="*", pattern="*", action="allow")],
        agent_rules=[{"permission": "read", "pattern": "*", "action": "allow"}],
        guard_rules=[Rule(permission="read", pattern="secret/**", action="deny")],
    )

    blocked = await hooks.authorize_tool("read", {"file_path": "secret/token.txt"})

    assert blocked is not None
    assert blocked.metadata["blocked"] is True
    assert "platform policy" in blocked.output


@pytest.mark.asyncio
async def test_platform_guard_retains_a_later_narrow_exception():
    hooks = ToolHooks(
        session_id="session-a",
        user_id="user-a",
        config_rules=[Rule(permission="*", pattern="*", action="allow")],
        guard_rules=[
            Rule(permission="read", pattern="private/**", action="deny"),
            Rule(permission="read", pattern="private/public/**", action="allow"),
        ],
    )

    denied = await hooks.authorize_tool("read", {"file_path": "private/key.txt"})
    allowed = await hooks.authorize_tool(
        "read", {"file_path": "private/public/readme.txt"}
    )

    assert denied is not None and denied.metadata["blocked"] is True
    assert allowed is None


@pytest.mark.asyncio
async def test_agent_allow_cannot_skip_a_platform_guard_ask(monkeypatch):
    monkeypatch.setattr(permission_mod.bus, "publish", lambda *_a, **_k: None)
    hooks = ToolHooks(
        session_id="session-a",
        user_id="user-a",
        config_rules=[Rule(permission="*", pattern="*", action="allow")],
        agent_rules=[{"permission": "read", "pattern": "*", "action": "allow"}],
        guard_rules=[
            Rule(permission="read", pattern="review/**", action="ask")
        ],
    )

    authorization = asyncio.create_task(
        hooks.authorize_tool("read", {"file_path": "review/report.txt"})
    )
    for _ in range(100):
        pending = permission_mod.list_pending("user-a")
        if pending:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("deployment guard ask was skipped by the Agent allow")

    assert pending[0].tool == "read"
    await permission_mod.reply(pending[0].id, "once", user_id="user-a")
    assert await authorization is None

    # A previously persisted user Always grant is still allowed to resolve the
    # guard's ask; it does not alter a guard deny.
    permission_mod._approved["user-a"] = [
        Rule(permission="read", pattern="*", action="allow")
    ]
    assert await hooks.authorize_tool(
        "read", {"file_path": "review/second.txt"}
    ) is None


def test_configured_permission_rules_are_available_as_non_bypassable_guards():
    config = SimpleNamespace(
        permission={
            "read": {
                "private/**": "deny",
                "private/public/**": "allow",
            }
        }
    )

    guards = _get_platform_guard_rules(config)

    assert [(rule.pattern, rule.action) for rule in guards] == [
        ("*", "ask"),
        ("private/**", "deny"),
        ("private/public/**", "allow"),
    ]


@pytest.mark.parametrize(
    "command",
    [
        "cat backend/.env",
        "python -c 'print(open(\"backend/.env.local\").read())'",
        "cat ~/.ssh/id_rsa",
        "cat ~/.config/service/credentials.json",
    ],
)
def test_bash_secret_reads_require_confirmation(command):
    config = SimpleNamespace(permission={})

    decision = permission_mod.evaluate("bash", command, _get_permission_rules(config))

    assert decision.action == "ask"


def test_ordinary_bash_command_requires_confirmation():
    config = SimpleNamespace(permission={})

    decision = permission_mod.evaluate(
        "bash", "pytest -q tests/unit", _get_permission_rules(config)
    )

    assert decision.action == "ask"


@pytest.mark.asyncio
async def test_ordinary_bash_executes_only_after_user_once_approval(monkeypatch):
    monkeypatch.setattr(permission_mod.bus, "publish", lambda *_a, **_k: None)
    hooks = ToolHooks(
        session_id="session-a",
        user_id="user-a",
        config_rules=_get_permission_rules(SimpleNamespace(permission={})),
        agent_rules=[{"permission": "bash", "pattern": "*", "action": "allow"}],
        guard_rules=_get_platform_guard_rules(SimpleNamespace(permission={})),
    )
    executed = []

    async def execute_fn(args, _ctx):
        executed.append(args["command"])
        return ToolResult(title="bash", output="ok")

    execution = asyncio.create_task(
        hooks.wrap_execute(
            "bash",
            execute_fn,
            {"command": "echo ordinary"},
            ToolContext(),
        )
    )
    for _ in range(100):
        pending = permission_mod.list_pending("user-a")
        if pending:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("ordinary Bash did not request permission")

    assert executed == []
    await permission_mod.reply(pending[0].id, "once", user_id="user-a")
    result = await execution

    assert executed == ["echo ordinary"]
    assert result.output == "ok"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".env", "ask"),
        ("/workspace/project/.env.local", "ask"),
        ("/workspace/project/.env.example", "allow"),
        (".ssh/id_ed25519", "ask"),
        ("/workspace/project/.ssh", "ask"),
        ("/home/runner/.ssh/id_ed25519", "ask"),
        ("config/service-credentials.json", "ask"),
        ("src/settings.py", "allow"),
    ],
)
def test_direct_reads_keep_secret_guards_at_any_directory_depth(path, expected):
    config = SimpleNamespace(permission={})

    decision = permission_mod.evaluate("read", path, _get_permission_rules(config))

    assert decision.action == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_id", "args"),
    [
        ("grep", {"pattern": ".", "path": "/workspace/project/.env"}),
        ("grep", {"pattern": ".", "path": "/home/runner/.ssh/id_ed25519"}),
        ("grep", {"pattern": ".", "path": "config/service-credentials.json"}),
        ("glob", {"pattern": "**/.env", "path": "/workspace/project"}),
    ],
)
async def test_read_like_search_tools_cannot_bypass_a_sensitive_path_deny(
    tool_id, args
):
    hooks = ToolHooks(
        session_id="session-a",
        user_id="user-a",
        config_rules=[
            *_get_permission_rules(SimpleNamespace(permission={})),
            Rule(permission="read", pattern="**.env**", action="deny"),
            Rule(permission="read", pattern="**/.ssh/**", action="deny"),
            Rule(permission="read", pattern="**credentials**", action="deny"),
        ],
    )

    blocked = await hooks.authorize_tool(tool_id, args)

    assert blocked is not None
    assert blocked.metadata["blocked"] is True


@pytest.mark.asyncio
async def test_batch_grep_reuses_the_same_sensitive_read_authorization(monkeypatch):
    import tool.registry as registry
    from tool.grep import grep_tool

    hooks = ToolHooks(
        session_id="session-a",
        user_id="user-a",
        config_rules=[
            *_get_permission_rules(SimpleNamespace(permission={})),
            Rule(permission="read", pattern="**.env**", action="deny"),
        ],
    )
    monkeypatch.setitem(registry._tools, "grep", grep_tool)
    ctx = ToolContext(
        available_tools=frozenset({"batch", "grep"}),
        _authorize_tool=hooks.authorize_tool,
    )

    result = await execute_batch(
        BatchArgs(
            invocations=[
                Invocation(
                    tool="grep",
                    parameters={"pattern": ".", "path": "/workspace/.env"},
                )
            ]
        ),
        ctx,
    )

    assert "Permission denied" in result.output
    assert "No matches" not in result.output
