"""Offline checks of the agent behaviour evaluation (tests/manual/agent_behavior_eval.py).

The judges, the metric extraction, the hard limits, the 25.5 decision rules
and the local sandbox are exercised directly; end-to-end runs drive the real
agent loop with the scripted fake model, including deliberately wrong scripts
that real tools carry out and the judges must reject. No provider is called.
"""
import dataclasses
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.manual import agent_behavior_eval as ev
from tests.manual import agent_behavior_tasks as tasks

BY_ID = {task.id: task for task in tasks.TASKS}


def make_env(tmp_path: Path, task=None) -> ev.TaskEnv:
    env = ev.TaskEnv(run_dir=tmp_path, sandbox=ev.LocalSandbox(tmp_path / "workspace"),
                     project_slug=(task.project_slug if task else "laojie"), user_id="u1")
    env.project_dir.mkdir(parents=True, exist_ok=True)
    if task is not None and task.setup is not None:
        task.setup(env)
    return env


def make_view(env, *, parts=(), calls=(), cards=(), finish=None) -> ev.RunView:
    return ev.RunView(env=env, metrics=ev.RunMetrics(), tool_parts=list(parts), provider_calls=list(calls),
                      final_text="", pending_questions=list(cards), finish=finish)


def apply_fake_writes(task, env) -> None:
    """Carry out the fake script's ``write`` calls directly (what a correct run leaves behind)."""
    for step in task.fake_script:
        for item in step if isinstance(step, list) else []:
            if item["tool"] == "write":
                path = item["args"]["file_path"].replace("{workdir}/", "")
                env.write(path, item["args"]["content"])


def provider_call(message_id, *tool_calls, main=True, usage=None, session="s1"):
    return ev.ProviderCall(session_id=session, message_id=message_id, main=main, visible_tools=[],
                           tool_calls=list(tool_calls), usage=usage or {})


def tc(tool, raw="{}", *, invalid=False, known=True, **args):
    return {"wire": tool, "tool": tool, "args": args, "arguments_raw": raw, "invalid": invalid, "known": known}


# -- the task set -------------------------------------------------------------

def test_task_set_follows_the_specification():
    tasks.check_task_set()
    assert len(tasks.TASKS) == 24
    assert {task.category for task in tasks.TASKS} == set(tasks.CATEGORIES)
    for task in tasks.TASKS:
        assert task.prompt and task.fake_script, task.id
        assert not task.skip_reason, task.id  # the merchant flow reaches its card offline
    assert all(task.judge == "human" and task.rubric for task in tasks.TASKS if task.category == "research")


# -- file judges ----------------------------------------------------------------

def _swap(env, a, b):
    first, second = env.project_dir / a, env.project_dir / b
    data = first.read_bytes()
    first.write_bytes(second.read_bytes())
    second.write_bytes(data)


def _rename_all(env):
    for stem, dish in tasks.PHOTOS.items():
        (env.project_dir / f"素材/{stem}.jpg").rename(env.project_dir / f"素材/{dish}.jpg")


FILE_CASES = {
    "files_sales_summary": (
        lambda env: env.write("销售/门店汇总.csv", tasks._sales_answer()),
        lambda env: env.write("销售/门店汇总.csv", "门店,销售额,单数\n" + "\n".join(
            reversed(tasks._sales_answer().strip().splitlines()[1:]))),
    ),
    "files_price_list_edit": (
        lambda env: apply_fake_writes(BY_ID["files_price_list_edit"], env),
        lambda env: (apply_fake_writes(BY_ID["files_price_list_edit"], env), env.write(
            "菜单/价目表.md", env.read("菜单/价目表.md").replace("10:00–21:00", "10:00–22:00"))),
    ),
    "files_meeting_todos": (
        lambda env: apply_fake_writes(BY_ID["files_meeting_todos"], env),
        lambda env: (apply_fake_writes(BY_ID["files_meeting_todos"], env), env.write(
            "会议记录/待办汇总.md", env.read("会议记录/待办汇总.md").replace("陈静", "某人"))),
    ),
    "files_members_export": (
        lambda env: apply_fake_writes(BY_ID["files_members_export"], env),
        lambda env: (apply_fake_writes(BY_ID["files_members_export"], env), env.write(
            "客户/高积分会员.csv", env.read("客户/高积分会员.csv").replace("138****5678", "13812345678"))),
    ),
    "files_rename_photos": (
        _rename_all,
        lambda env: (_rename_all(env), _swap(env, "素材/红烧肉.jpg", "素材/宫保鸡丁.jpg")),
    ),
}


@pytest.mark.parametrize("task_id", sorted(FILE_CASES))
def test_file_judges_accept_the_result_and_reject_a_near_miss(tmp_path, task_id):
    task = BY_ID[task_id]
    good, bad = FILE_CASES[task_id]
    untouched = make_env(tmp_path / "untouched", task)
    assert task.judge(make_view(untouched)).passed is False
    right = make_env(tmp_path / "right", task)
    good(right)
    verdict = task.judge(make_view(right))
    assert verdict.passed is True, verdict.detail
    wrong = make_env(tmp_path / "wrong", task)
    bad(wrong)
    assert task.judge(make_view(wrong)).passed is False


# -- coding judges run the project's own tests -----------------------------------

@pytest.mark.parametrize("task_id", [task.id for task in tasks.TASKS if task.category == "coding"])
def test_coding_judges_run_the_tests_and_protect_them(tmp_path, task_id):
    task = BY_ID[task_id]
    broken = make_env(tmp_path / "broken", task)
    assert task.judge(make_view(broken)).passed is False  # the bug reproduces
    fixed = make_env(tmp_path / "fixed", task)
    apply_fake_writes(task, fixed)
    verdict = task.judge(make_view(fixed))
    assert verdict.passed is True, verdict.detail
    test_file = next(path for path in fixed.project_dir.rglob("test_*.py"))
    test_file.write_text(test_file.read_text(encoding="utf-8") + "\n# weakened\n", encoding="utf-8")
    assert "test files changed" in task.judge(make_view(fixed)).detail


# -- schedule judges ----------------------------------------------------------------

@pytest.mark.parametrize(("task_id", "schedule", "text", "passed"), [
    ("cron_daily_bad_reviews", {"kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Shanghai"}, "看外卖差评", True),
    ("cron_daily_bad_reviews", {"kind": "cron", "expr": "0 1 * * *", "tz": "UTC"}, "看外卖差评", True),
    ("cron_daily_bad_reviews", {"kind": "cron", "expr": "0 9 * * 1-5", "tz": "Asia/Shanghai"}, "看外卖差评", False),
    ("cron_daily_bad_reviews", {"kind": "every", "every_ms": 86_400_000}, "看外卖差评", False),
    ("cron_daily_bad_reviews", {"kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Shanghai"}, "提醒一下", False),
    ("cron_weekly_sales_report", {"kind": "cron", "expr": "0 10 * * 1", "tz": "Asia/Shanghai"},
     "读取 /workspace/laojie/销售/每日流水.csv，写上周销售周报", True),
    ("cron_weekly_sales_report", {"kind": "cron", "expr": "0 2 * * MON", "tz": "UTC"}, "按 销售/每日流水.csv 出周报", True),
    ("cron_weekly_sales_report", {"kind": "cron", "expr": "0 10 * * 0", "tz": "Asia/Shanghai"},
     "读取 销售/每日流水.csv，写销售周报", False),
    ("cron_weekly_sales_report", {"kind": "cron", "expr": "0 10 * * *", "tz": "Asia/Shanghai"},
     "读取 销售/每日流水.csv，写销售周报", False),
    ("cron_weekly_sales_report", {"kind": "cron", "expr": "0 9 * * 1", "tz": "Asia/Shanghai"},
     "读取 销售/每日流水.csv，写销售周报", False),
    # Right time, but the run would not know where the figures are.
    ("cron_weekly_sales_report", {"kind": "cron", "expr": "0 10 * * 1", "tz": "Asia/Shanghai"}, "销售周报", False),
    ("cron_workday_supplier_order", {"kind": "cron", "expr": "0 15 * * MON-FRI", "tz": "Asia/Shanghai"}, "给供应商下单", True),
    ("cron_workday_supplier_order", {"kind": "cron", "expr": "0 15 * * *", "tz": "Asia/Shanghai"}, "给供应商下单", False),
    ("cron_workday_supplier_order", {"kind": "cron", "expr": "30 15 * * 1-5", "tz": "Asia/Shanghai"}, "给供应商下单", False),
])
async def test_cron_judges_check_the_fire_times(tmp_path, monkeypatch, task_id, schedule, text, passed):
    job = SimpleNamespace(enabled=True, name="提醒", schedule=schedule, task_prompt=text)
    disabled = SimpleNamespace(enabled=False, name="旧的", schedule=schedule, task_prompt=text)

    async def jobs(_user_id):
        return [disabled, job]
    monkeypatch.setattr(tasks, "_cron_jobs", jobs)
    verdict = await BY_ID[task_id].judge(make_view(make_env(tmp_path)))
    assert verdict.passed is passed, verdict.detail


async def test_weekly_report_may_name_a_script_that_reads_the_data(tmp_path, monkeypatch):
    """A task that runs a report script the agent wrote knows its data through that script."""
    job = SimpleNamespace(enabled=True, name="销售周报", task_prompt="运行 python3 scripts/weekly_report.py，把输出的周报发出来",
                          schedule={"kind": "cron", "expr": "0 10 * * 1", "tz": "Asia/Shanghai"})

    async def jobs(_user_id):
        return [job]
    monkeypatch.setattr(tasks, "_cron_jobs", jobs)
    judge = BY_ID["cron_weekly_sales_report"].judge
    env = make_env(tmp_path, BY_ID["cron_weekly_sales_report"])
    assert "每日流水" in (await judge(make_view(env))).detail  # no such script yet
    env.write("scripts/weekly_report.py", "rows = open('销售/每日流水.csv').read()\n")
    verdict = await judge(make_view(env))
    assert verdict.passed is True, verdict.detail
    env.write("scripts/weekly_report.py", "rows = open('销售/别的表.csv').read()\n")
    assert (await judge(make_view(env))).passed is False


def test_weekly_report_has_the_data_its_prompt_names(tmp_path):
    task = BY_ID["cron_weekly_sales_report"]
    assert tasks.DAILY_TAKINGS_FILE in task.prompt
    rows = tasks._read_csv(make_env(tmp_path, task).project_dir / tasks.DAILY_TAKINGS_FILE)
    assert rows[0] == ["日期", "门店", "单数", "销售额"] and len(rows) == 1 + 14 * 3
    days = sorted({row[0] for row in rows[1:]})
    assert (days[0], days[-1]) == ("2026-11-09", "2026-11-22")  # two whole Monday-to-Sunday weeks
    assert all(int(row[2]) > 0 and tasks._number(row[3]) > 0 for row in rows[1:])


async def test_cron_judge_needs_a_job(tmp_path, monkeypatch):
    async def none(_user_id):
        return []
    monkeypatch.setattr(tasks, "_cron_jobs", none)
    assert (await BY_ID["cron_daily_bad_reviews"].judge(make_view(make_env(tmp_path)))).passed is False


def _confirm_card(text, status="pending"):
    return {"id": "q1", "status": status, "answers": None, "continuation": {},
            "questions": [{"question": text, "options": [{"label": "确认创建"}]}]}


@pytest.mark.parametrize("task_id,text,passed", [
    ("cron_daily_bad_reviews", "将创建「外卖差评提醒」：每天 09:00 提醒你查看前一天的外卖差评并逐条回复。确认吗？", True),
    ("cron_daily_bad_reviews", "将创建「外卖差评提醒」：每周一 09:00 提醒你查看差评。确认吗？", False),
    ("cron_daily_bad_reviews", "将创建「提醒」：每天 9点 提醒你。确认吗？", False),
    ("cron_weekly_sales_report", "将创建「销售周报」：每周一 10:00 读取 销售/每日流水.csv 整理上周周报。确认吗？", True),
    ("cron_weekly_sales_report", "将创建「销售周报」：每周一 10:00 整理上周周报。确认吗？", False),
    ("cron_workday_supplier_order", "将创建「供应商下单提醒」：工作日 15:00 提醒你给供应商下单。确认吗？", True),
    ("cron_workday_supplier_order", "将创建「供应商下单提醒」：每天 15:00 提醒你给供应商下单。确认吗？", False),
])
async def test_cron_judge_accepts_the_skills_confirmation_card(tmp_path, monkeypatch, task_id, text, passed):
    async def none(_user_id):
        return []
    monkeypatch.setattr(tasks, "_cron_jobs", none)
    task = BY_ID[task_id]
    verdict = await task.judge(make_view(make_env(tmp_path, task), cards=[_confirm_card(text)]))
    assert verdict.passed is passed, verdict.detail
    answered = await task.judge(make_view(make_env(tmp_path, task), cards=[_confirm_card(text, status="answered")]))
    assert answered.passed is False


# -- merchant judges: the card, its parameters, nothing paid -------------------------

def card(*lines, status="pending"):
    return {"id": "q1", "status": status, "answers": None, "continuation": {},
            "questions": [{"question": "\n".join(lines) + "\n\n菜名和价格对吗？", "options": [{"label": "对"}]}]}


MENU_CARD = card("老街面馆", "招牌牛肉面 22 元", "酸辣粉 15 元", "卤蛋 3 元", "冰粉 8 元")


def test_paid_card_judge(tmp_path):
    judge = BY_ID["merchant_menu_review"].judge
    env = make_env(tmp_path)
    assert judge(make_view(env, cards=[MENU_CARD], finish="waiting_input")).passed is True
    wrong_price = card("招牌牛肉面 18 元", "酸辣粉 15 元", "卤蛋 3 元", "冰粉 8 元")
    assert "牛肉面 22" in judge(make_view(env, cards=[wrong_price])).detail
    assert judge(make_view(env, cards=[card("招牌牛肉面 22 元")])).passed is False
    assert judge(make_view(env, cards=[{**MENU_CARD, "status": "answered"}])).passed is False
    generated = [{"tool": "image_gen", "status": "completed", "metadata": {}}]
    assert judge(make_view(env, parts=generated, cards=[MENU_CARD])).passed is False
    turned_back = [{"tool": "image_gen", "status": "error", "metadata": {"blocked": True}}]
    verdict = judge(make_view(env, parts=turned_back, cards=[MENU_CARD]))
    assert verdict.passed is True and "turned back" in verdict.detail
    video = BY_ID["merchant_video_light"].judge
    assert video(make_view(env, cards=[card("画面：牛肉面特写", "时长：15 秒竖屏", "店名：老街面馆")])).passed
    assert not video(make_view(env, cards=[card("画面：牛肉面特写", "时长：10 秒竖屏", "店名：老街面馆")])).passed


def test_first_step_skill_judge(tmp_path):
    judge = BY_ID["skill_mention_poster"].judge
    env = make_env(tmp_path)
    first = provider_call("m1", tc("skill", skill="poster-maker"), tc("example_pick"))
    assert judge(make_view(env, calls=[first])).passed is True
    searched_first = provider_call("m1", tc("skill_search", query="海报"))
    later = provider_call("m2", tc("skill", skill="poster-maker"))
    assert judge(make_view(env, calls=[searched_first, later])).passed is False
    assert judge(make_view(env, calls=[provider_call("m1", tc("skill", skill="imagegen"))])).passed is False
    assert judge(make_view(env)).passed is False


# -- assistant judges (the placement evaluation's rules) -----------------------------

async def test_assistant_judges(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    env.extra["north_star"] = "p-north"
    env.state.update({"linked": {"task_id": "t-1", "task_revision": 1}, "before": [2, 2, 1],
                      "north_star_sessions": 1})
    after = {"value": [2, 2, 1], "north_star": 1}

    async def counts(_owner):
        return after["value"]

    async def sessions_in(_owner, project_id):
        assert project_id == "p-north"
        return after["north_star"]
    monkeypatch.setattr(tasks, "_assistant_counts", counts)
    monkeypatch.setattr(tasks, "_sessions_in", sessions_in)
    followup = {"tool": "tasks.followup", "input": {"task_id": "t-1"}, "output": json.dumps({"state": "accepted"})}
    assert (await BY_ID["assistant_followup_budget"].judge(make_view(env, parts=[followup]))).passed is True
    other = {**followup, "input": {"task_id": "t-2"}}
    assert (await BY_ID["assistant_followup_budget"].judge(make_view(env, parts=[other]))).passed is False
    submit = {"tool": "tasks.submit", "input": {"project_id": "p-north", "destination_confirmed": True},
              "output": json.dumps({"state": "accepted"})}
    judge = BY_ID["assistant_new_conversation"].judge
    assert (await judge(make_view(env, parts=[submit]))).passed is False  # no new conversation yet
    after.update(value=[2, 3, 2], north_star=2)
    verdict = await judge(make_view(env, parts=[submit]))
    assert verdict.passed is True and "new conversations in North Star 1" in verdict.detail
    after["value"] = [3, 3, 2]  # a new project as well: not what was asked
    assert (await judge(make_view(env, parts=[submit]))).passed is False
    after.update(value=[2, 3, 2], north_star=1)  # the new conversation landed outside North Star
    assert (await judge(make_view(env, parts=[submit]))).passed is False
    after["north_star"] = 2
    elsewhere = {**submit, "input": {"project_id": "p-default", "destination_confirmed": True}}
    assert (await judge(make_view(env, parts=[elsewhere]))).passed is False
    linked = {"tool": "tasks.followup", "input": {"task_id": "t-1"}, "output": json.dumps({"state": "accepted"})}
    assert (await judge(make_view(env, parts=[linked]))).passed is False  # continued the old one instead


async def test_assistant_new_conversation_stopped_at_a_card_fails(tmp_path, monkeypatch):
    """A submit that raised its own card created nothing, and says which card it was."""
    env = make_env(tmp_path)
    env.extra["north_star"] = "p-north"
    env.state.update({"before": [2, 2, 1], "north_star_sessions": 1})

    async def counts(_owner):
        return [2, 2, 1]

    async def sessions_in(_owner, _project_id):
        return 1
    monkeypatch.setattr(tasks, "_assistant_counts", counts)
    monkeypatch.setattr(tasks, "_sessions_in", sessions_in)
    waiting = {"tool": "tasks.submit", "status": "waiting_input", "output": "",
               "input": {"project_id": "p-north", "destination_confirmed": True}}
    for header in ("任务 AI 模型", "安排这件事"):
        model_card = {"id": "q1", "status": "pending", "answers": None, "continuation": {},
                      "questions": [{"header": header, "question": "？", "options": [{"label": "取消"}]}]}
        verdict = await BY_ID["assistant_new_conversation"].judge(
            make_view(env, parts=[waiting], cards=[model_card], finish="waiting_input"))
        assert verdict.passed is False
        assert "('tasks.submit', 'waiting_input')" in verdict.detail and header in verdict.detail


def test_evaluation_config_offers_the_evaluated_model(monkeypatch):
    """The assistant arranges new work with the default task AI only if the catalogue offers it."""
    import core.config
    from api.metadata import _chat_models
    from core.config import ModelConfig, OpenBoxConfig
    deployed = OpenBoxConfig(model="openai/deployed", models=[ModelConfig(id="openai/deployed"),
                                                              ModelConfig(id="openai/other")])
    monkeypatch.setattr(core.config, "_config", deployed)
    config = ev.evaluation_config(ev.EvalOptions(model="openai/evaluated"))
    assert config.model == "openai/evaluated"
    assert [entry.id for entry in config.models] == ["openai/evaluated", "openai/deployed", "openai/other"]
    offered = _chat_models(config, context_limit=lambda _model: 1, supports_vision=lambda _model: False)
    assert config.model in {row["id"] for row in offered}
    assert [entry.id for entry in deployed.models] == ["openai/deployed", "openai/other"]  # a copy
    listed = ev.evaluation_config(ev.EvalOptions(model="openai/other"))
    assert [entry.id for entry in listed.models] == ["openai/deployed", "openai/other"]
    assert ev.evaluation_config(ev.EvalOptions(fake_model=True)).models == []


# -- metrics ------------------------------------------------------------------------

def test_metric_extraction():
    calls = [
        provider_call("m1", tc("skill", skill="menu-maker"), tc("read", '{"file_path": "a"}'),
                      usage={"input": 100, "output": 10, "total": 110}),
        # The same step retried: counts once, the retry's calls replace the failed attempt's.
        provider_call("m2", tc("bash"), usage={"input": 50, "output": 5}),
        provider_call("m2", tc("capability_search"), tc("question"), tc("web_search", invalid=True, known=False),
                      usage={"input": 120, "output": 20, "total": 140}),
        provider_call("m3", tc("write", '{"file_path": "x"}{"file_path": "y"}'), tc("made_up", invalid=True, known=False),
                      usage={"input": 10, "output": 1, "total": 11}),
        provider_call("child", tc("read"), main=False, session="s2", usage={"input": 30, "output": 3, "total": 33}),
        provider_call("m4", usage={"input": 5, "output": 2, "total": 7}),
    ]
    parts = [
        {"tool": "skill", "status": "completed", "input": {"skill": "menu-maker"}, "title": "Loaded skill: menu-maker"},
        {"tool": "skill", "status": "completed", "input": {"skill": "menu-maker"}, "title": "Loaded skill: menu-maker"},
        {"tool": "skill", "status": "completed", "input": {"skill": "menu-maker"}, "title": "Loaded skill: menu-maker"},
        {"tool": "skill", "status": "completed", "input": {"skill": "imagegen"}, "title": "Loaded skill: imagegen"},
        {"tool": "skill", "status": "completed", "input": {"skill": "nope"}, "title": "Skill not found: nope"},
        {"tool": "cron", "status": "completed", "metadata": {"error": True, "error_code": "invalid_arguments"}},
    ]
    metrics = ev.extract_metrics(calls, parts, registry_ids={"skill", "read", "bash", "web_search", "write",
                                                             "question", "capability_search", "cron"})
    assert metrics.steps == 4
    assert metrics.tool_calls_per_step == [2, 3, 2, 0]
    assert metrics.tool_calls == 7
    assert metrics.first_step_tools == ["skill", "read"]
    assert metrics.question_calls == 1
    assert metrics.capability_search_calls == 1
    assert metrics.unmaterialized_tool_calls == 1  # web_search exists but was not revealed
    assert sorted(item["kind"] for item in metrics.malformed_detail) == [
        "arguments_not_json", "schema_rejected", "unknown_tool_name"]
    assert metrics.malformed_tool_calls == 3
    assert metrics.skill_loads == {"menu-maker": 3, "imagegen": 1}
    assert metrics.repeated_skill_loads == 2
    assert metrics.output_tokens == 10 + 5 + 20 + 1 + 3 + 2
    assert metrics.total_tokens == 110 + 55 + 140 + 11 + 33 + 7
    assert metrics.subagent_requests == 1


async def _drain(stream):
    return [event async for event in stream]


async def test_observed_provider_enforces_the_step_and_token_limits():
    async def upstream(**kwargs):
        yield {"type": "tool_call", "tool": "read", "args": {}, "arguments_raw": "{}", "call_id": "c"}
        yield {"type": "finish", "reason": "tool_calls", "usage": {"input": 40, "output": 10, "total": 50}}

    def ctx(message_id, session="main"):
        return SimpleNamespace(session_id=session, message_id=message_id)

    tools = {"read": SimpleNamespace(id="read")}
    observed = ev.ObservedProvider(main_session_id="main", upstream=upstream, max_steps=2, max_tokens=10_000)
    for message_id in ("m1", "m1", "m2"):  # a retry of m1 is not a new step
        events = await _drain(observed(ctx=ctx(message_id), tools=tools, model_id="openai/x"))
        assert events[-1]["type"] == "finish"
    refused = await _drain(observed(ctx=ctx("m3"), tools=tools))
    assert refused[0]["type"] == "error" and observed.limit_hit == "steps"
    assert observed.main_steps() == 2 and observed.calls[0].model_id == "openai/x"

    by_tokens = ev.ObservedProvider(main_session_id="main", upstream=upstream, max_steps=30, max_tokens=100)
    await _drain(by_tokens(ctx=ctx("m1"), tools=tools))
    await _drain(by_tokens(ctx=ctx("child", session="sub"), tools=tools))  # subagent tokens count too
    refused = await _drain(by_tokens(ctx=ctx("m2"), tools=tools))
    assert refused[0]["type"] == "error" and by_tokens.limit_hit == "tokens"


async def test_fake_model_reveals_hidden_tools_before_calling_them():
    capability = SimpleNamespace(id="capability_search")
    fake = ev.FakeModel([[ev.call("cron", action="list")], ev.say("好了 {workdir}")], {"workdir": "/workspace/p"})
    first = await _drain(fake(tools={"capability_search": capability}, messages=[]))
    assert first[0]["tool"] == "capability_search" and first[0]["args"]["names"] == ["cron"]
    second = await _drain(fake(tools={"capability_search": capability, "cron": SimpleNamespace(id="cron")}, messages=[]))
    assert second[0]["tool"] == "cron" and second[-1]["reason"] == "tool_calls"
    third = await _drain(fake(tools={}, messages=[]))
    assert third[0] == {"type": "text_delta", "text": "好了 /workspace/p"} and third[-1]["reason"] == "stop"


# -- decision rules ------------------------------------------------------------------

def result(*runs):
    return {"runs": list(runs)}


def run(task, *, completed=True, steps=4, calls=4, questions=0, malformed=0, tokens=10_000):
    return {"task": task, "completed": completed, "metrics": {
        "steps": steps, "tool_calls": calls, "question_calls": questions, "capability_search_calls": 0,
        "repeated_skill_loads": 0, "malformed_tool_calls": malformed, "unmaterialized_tool_calls": 0,
        "output_tokens": 0, "total_tokens": tokens}}


def verdicts(rules):
    return {rule["rule"]: rule["passed"] for rule in rules}


def test_compare_applies_the_decision_rules():
    base_1 = result(run("a"), run("b", completed=False, steps=6, questions=1))
    base_2 = result(run("a"), run("b", steps=5))
    good = result(run("a", steps=3, calls=6), run("b", completed=False, steps=4, calls=6))
    assert verdicts(ev.compare([base_1, base_2], good, a1=True)) == {
        "completion": True, "steps_plus_questions": True, "a1_tool_calls_per_step": True,
        "malformed_tool_calls": True, "total_tokens": True}

    worse = result(run("a", completed=False), run("b", completed=False, steps=8, questions=2, malformed=1,
                                                  tokens=20_000))
    assert verdicts(ev.compare([base_1, base_2], worse, a1=True)) == {
        "completion": False, "steps_plus_questions": False, "a1_tool_calls_per_step": False,
        "malformed_tool_calls": False, "total_tokens": False}

    # 115% of the higher baseline is the line; one baseline file stands for both rounds.
    edge = result(run("a", tokens=11_500), run("b", tokens=11_500, steps=5))
    assert verdicts(ev.compare([base_2], edge))["total_tokens"] is True
    over = result(run("a", tokens=11_600), run("b", tokens=11_600, steps=5))
    assert verdicts(ev.compare([base_2], over))["total_tokens"] is False


def test_compare_leaves_out_pending_human_verdicts_and_unshared_tasks():
    base = result(run("a"), run("research", completed=True))
    candidate = result(run("a"), run("research", completed=None), run("extra", completed=False))
    rules = ev.compare([base], candidate)
    assert verdicts(rules)["completion"] is True
    assert any(rule["rule"] == "pending_human" for rule in rules)
    assert ev.compare([base], result(run("a", completed=None)))[0]["passed"] is None


def test_compare_command_line(tmp_path, capsys):
    paths = []
    for name, data in (("b1", result(run("a"))), ("b2", result(run("a"))), ("ok", result(run("a"))),
                       ("bad", result(run("a", malformed=2)))):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(data))
        paths.append(str(path))
    assert ev.main(["--compare", paths[0], paths[1], paths[2]]) == 0
    assert ev.main(["--compare", paths[0], paths[3]]) == 1
    assert "FAIL  malformed_tool_calls" in capsys.readouterr().out


def test_summary_table_covers_every_task():
    data = {"model": "m", "exposure": "portable", "tuning": [], "fake_model": True, "label": "x",
            "runs": [run("files_a"), {**run("research_b", completed=None), "judge": "human"},
                     {"task": "skipped_c", "skipped": "needs a desktop"}]}
    table = ev.summary_table(data)
    assert "files_a" in table and "human" in table and "skipped: needs a desktop" in table


# -- the local sandbox ------------------------------------------------------------------

async def test_local_sandbox_maps_workspace_and_keeps_secrets_out(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-for-the-model")
    box = ev.LocalSandbox(tmp_path / "workspace")
    await box.write_file("/workspace/p/a.txt", "一\n二\n")
    assert (tmp_path / "workspace/p/a.txt").read_text() == "一\n二\n"
    assert await box.read_file("/workspace/p/a.txt") == "     1\t一\n     2\t二\n     3\t"
    assert await box.read_file_if_exists("/workspace/p/missing.txt") is None
    with pytest.raises(PermissionError):
        box.host("/workspace/../outside")
    with pytest.raises(FileNotFoundError):
        box.host("/etc/passwd")
    shell = await box.execute("cat /workspace/p/a.txt && pwd && env", workdir="/workspace/p")
    assert shell.exit_code == 0 and "一" in shell.stdout and "/workspace/p" in shell.stdout
    assert "not-for-the-model" not in shell.stdout and str(tmp_path) not in shell.stdout
    assert await box.glob("**/*.txt", "/workspace") == ["/workspace/p/a.txt"]
    resolved = await box.resolve_paths([SimpleNamespace(path="/workspace/p/new.txt", allow_missing=True,
                                                        allow_scoped_skills=False)])
    assert resolved[0].canonical_path == "/workspace/p/new.txt" and resolved[0].workspace_relative == "p/new.txt"
    stat = await box.execute("stat -c %Y -- '/workspace/p/a.txt' 2>/dev/null")
    assert stat.exit_code == 0 and stat.stdout.strip().isdigit()


@pytest.mark.skipif(not (os.uname().sysname == "Darwin" and shutil.which("sandbox-exec")),
                    reason="write confinement uses macOS sandbox-exec")
async def test_local_sandbox_confines_writes_on_macos(tmp_path):
    box = ev.LocalSandbox(tmp_path / "run/workspace")
    outside = tmp_path / "outside.txt"
    result = await box.execute(f"echo x > {outside}")
    assert result.exit_code != 0 and not outside.exists()


# -- one end-to-end run with the fake model -------------------------------------------

async def test_fake_model_drives_the_real_loop_end_to_end(tmp_path, monkeypatch):
    import core.config
    from agent import llm

    async def no_provider(*_args, **_kwargs):
        raise AssertionError("the evaluation must not call a provider in --fake-model mode")
        yield  # pragma: no cover
    monkeypatch.setattr(llm, "stream_llm", no_provider)
    monkeypatch.setattr(llm, "_stream_litellm_direct", no_provider)
    config_before, cwd_before = core.config._config, Path.cwd()
    selected = [BY_ID["files_sales_summary"], BY_ID["merchant_menu_review"]]
    output = tmp_path / "result.json"
    data = await ev.run_suite(selected, ev.EvalOptions(fake_model=True, repeats=1), output)

    assert json.loads(output.read_text())["aggregate"] == data["aggregate"]
    files, merchant = data["runs"]
    assert files["completed"] is True, files
    assert files["metrics"]["steps"] == 3 and files["metrics"]["tool_calls_per_step"] == [1, 1, 0]
    assert files["tools"] == ["read", "write"] and files["finish"] == "stop"
    assert merchant["completed"] is True, merchant
    assert merchant["finish"] == "waiting_input"
    assert merchant["metrics"]["question_calls"] == 1
    assert merchant["metrics"]["skill_loads"] == {"menu-maker": 1}
    # Portable exposure hides example_pick until it is searched for.
    assert merchant["metrics"]["capability_search_calls"] == 1
    for record in data["runs"]:
        assert record["metrics"]["malformed_tool_calls"] == 0 and not record["error"]
        assert record["metrics"]["total_tokens"] > 0 and record["wall_seconds"] > 0
    assert data["aggregate"]["completed"] == 2
    assert core.config._config is config_before and Path.cwd() == cwd_before


def _submit(**extra):
    """A tasks.submit step for the North Star request, varied by ``extra``."""
    def step(kwargs, env):
        args = {"title": "夏季新品上市方案", "instructions": "写一份夏季新品上市方案。",
                "source_message_ids": [tasks._original_human(kwargs)], **extra}
        if args.get("project_id") == "north_star":
            args["project_id"] = env.extra["north_star"]
        return [ev.call("tasks.submit", **args)]
    return tasks.dynamic(step)


WEEKLY = "0 10 * * 1"
SCRIPTED_RUNS = [
    # (task, script, judged complete): the real tools carry each script out.
    ("assistant_new_conversation", None, True),
    ("assistant_new_conversation", [_submit(project_id="north_star", let_user_choose=True), ev.say("选一下")], False),
    ("assistant_new_conversation", [_submit(placement={"question": "新建项目？", "project_name": "夏季新品"}),
                                    ev.say("新建了项目")], False),
    ("assistant_new_conversation", [tasks.dynamic(tasks._fake_followup), ev.say("接着原来的对话做")], False),
    ("cron_weekly_sales_report", None, True),
    ("cron_weekly_sales_report", [[ev.call("cron", action="add", name="销售周报", schedule="0 10 * * *",
                                           task="读取 销售/每日流水.csv，写上周销售周报。")], ev.say("好了")], False),
    ("cron_weekly_sales_report", [[ev.call("cron", action="add", name="销售周报", schedule=WEEKLY,
                                           task="整理上周的销售情况，写成周报。")], ev.say("好了")], False),
    ("cron_weekly_sales_report", [[ev.call("question", questions=[{
        "header": "数据来源", "question": "上周的销售数据从哪里来？",
        "options": [{"label": "上传表格"}, {"label": "从后台拉"}]}])], ev.say("（不应到达）")], False),
]


async def test_scripted_runs_through_the_real_tools_are_judged(monkeypatch):
    """The fixed tasks pass their own script and reject wrong placement, a wrong day or a missing data source."""
    from agent import llm

    async def no_provider(*_args, **_kwargs):
        raise AssertionError("the evaluation must not call a provider in --fake-model mode")
        yield  # pragma: no cover
    monkeypatch.setattr(llm, "stream_llm", no_provider)
    monkeypatch.setattr(llm, "_stream_litellm_direct", no_provider)
    selected = [dataclasses.replace(BY_ID[task_id], id=f"{task_id}-{index}",
                                    fake_script=script if script is not None else BY_ID[task_id].fake_script)
                for index, (task_id, script, _expected) in enumerate(SCRIPTED_RUNS)]
    data = await ev.run_suite(selected, ev.EvalOptions(fake_model=True, repeats=1))
    for (task_id, _script, expected), record in zip(SCRIPTED_RUNS, data["runs"]):
        assert record["completed"] is expected, (record["task"], record["judge_detail"], record["tool_trace"])
        assert not record["error"] and not record.get("harness_error"), record
    placement_card = data["runs"][1]
    assert placement_card["finish"] == "waiting_input" and placement_card["cards"][0]["headers"] == ["安排这件事"]
    assert "pending cards ['安排这件事']" in placement_card["judge_detail"]
    assert "[2, 2, 1] -> [3, 3, 2]" in data["runs"][2]["judge_detail"]  # a new project nobody asked for
    assert "每日流水" in data["runs"][6]["judge_detail"]
