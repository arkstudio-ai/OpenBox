"""desktop_takeover: hand a browser task to the user when a site challenges the agent.

Captchas, slider puzzles, SMS codes and "abnormal environment" risk-control
pages are designed to stop automation, and the product rule is that the agent
never tries to beat them. What it can do is stop cleanly and put the user in
front of the page: this tool files a blocking question whose structured
`detail` the frontend renders as a takeover card, with a link that opens the
cloud desktop panel with input control already switched on. The run stays
suspended until the user reports back, so the model resumes on the same page
the user just unblocked.

The browser matters. In `local` mode the page is on the cloud desktop and the
card can jump there; in `extension` mode it is on the user's own machine, so
the card only tells them to finish it in their browser.
"""
from typing import Literal

from pydantic import BaseModel, Field

from core.log import create_logger
from question import question as question_mod
from question.question import Question, QuestionOption
from tool.tool import ToolResult, ToolContext, define_tool

log = create_logger("tool.desktop_takeover")

TakeoverReason = Literal[
    "captcha_slider",
    "captcha_click",
    "captcha_math",
    "sms_code",
    "login_required",
    "risk_control",
    "other",
]

#: Answer labels. The first is the recommended way out; the frontend renders
#: them as pills, so keep them short.
ANSWER_DONE = "我已完成，继续"
ANSWER_SKIP = "跳过这一步"
ANSWER_ABANDON = "放弃任务"

_REASON_TEXT: dict[str, str] = {
    "captcha_slider": "页面弹出了滑块/拼图验证，需要人工拖动完成。",
    "captcha_click": "页面要求按顺序点选图中的文字或图片，需要人工完成。",
    "captcha_math": "页面要求输入算术题或图形验证码的答案，需要人工完成。",
    "sms_code": "页面要求输入发到你手机/邮箱的验证码，只有你能收到。",
    "login_required": "页面要求登录，且登录过程带有人机验证，需要你亲自完成。",
    "risk_control": "网站触发了风控（安全验证 / 环境异常 / 访问频繁），需要人工通过验证。",
    "other": "页面出现了自动化无法通过的人工验证步骤。",
}


class DesktopTakeoverArgs(BaseModel):
    reason: TakeoverReason = Field(
        description=(
            "What blocked you: captcha_slider (drag/puzzle), captcha_click (pick the "
            "characters/images), captcha_math (type the code or answer), sms_code "
            "(one-time code sent to the user), login_required (a login wall that "
            "itself carries a human check), risk_control (security verification / "
            "abnormal environment / too many requests), or other."
        )
    )
    url: str = Field(default="", max_length=2000, description="URL of the blocked page, for display only.")
    page: str = Field(
        default="",
        max_length=100,
        description="The dev-browser page name you were driving (e.g. \"checkout\"), so you return to it.",
    )
    instructions: str = Field(
        default="",
        max_length=300,
        description="One or two sentences telling the user what to do on the page, in their language.",
    )


def _host(url: str) -> str:
    try:
        from urllib.parse import urlsplit
        return urlsplit(url).hostname or ""
    except Exception:
        return ""


async def _browser_location(ctx: ToolContext) -> str:
    """Where the blocked page lives: "local" (cloud desktop) or "extension" (user's own Chrome)."""
    if not ctx.sandbox:
        return "extension"
    try:
        from sandbox.browser import browser_status
        status = await browser_status(ctx.sandbox)
        relay = (status or {}).get("relay") or {}
        mode = str(relay.get("mode") or "")
        if mode:
            return "extension" if mode == "extension" else "local"
    except Exception as e:
        log.debug(f"browser status probe skipped: {e}")
    # The relay is not up, so whatever blocked the model was on the desktop's
    # own browser (a headless or foreground local Chrome).
    return "local"


def _question_text(args: DesktopTakeoverArgs, browser: str) -> str:
    lines = [_REASON_TEXT.get(args.reason, _REASON_TEXT["other"])]
    host = _host(args.url)
    if host:
        lines.append(f"站点：{host}")
    if args.instructions.strip():
        lines.append(args.instructions.strip())
    if browser == "local":
        lines.append("请打开云桌面完成验证，完成后回来点「我已完成，继续」。")
    else:
        lines.append("验证页在你自己的浏览器里，请在那里完成，完成后回来点「我已完成，继续」。")
    return "\n".join(lines)


async def execute(args: DesktopTakeoverArgs, ctx: ToolContext) -> ToolResult:
    browser = await _browser_location(ctx)
    detail = {
        "kind": "desktop_takeover",
        "reason": args.reason,
        "url": args.url,
        "host": _host(args.url),
        "page": args.page,
        "instructions": args.instructions.strip(),
        "browser": browser,
    }
    question_text = _question_text(args, browser)

    # QuestionRejectedError propagates: the tool hooks turn it into a
    # "Rejected" result, which the description tells the model how to read.
    answers = await question_mod.ask(
        session_id=ctx.session_id,
        user_id=ctx.user_id or "default",
        questions=[
            Question(
                header="需要你接管",
                question=question_text,
                options=[
                    QuestionOption(label=ANSWER_DONE, description="验证已通过，让 agent 从当前页面继续"),
                    QuestionOption(label=ANSWER_SKIP, description="不做这一步，让 agent 换个办法或跳过"),
                    QuestionOption(label=ANSWER_ABANDON, description="停止这个浏览器任务"),
                ],
                multiple=False,
                custom=True,
                detail=detail,
            )
        ],
        tool={"messageID": ctx.message_id, "callID": ctx.part_id} if ctx.part_id else None,
    )

    answer = (answers[0][0] if answers and answers[0] else "").strip()
    page_hint = f"client.page({args.page!r})" if args.page else "the same page"

    if answer == ANSWER_ABANDON:
        output = (
            "The user chose to abandon this browser task. Stop working on it, do not "
            "retry the challenge, and tell the user what was and was not done."
        )
        title = "用户放弃了该任务"
    elif answer == ANSWER_SKIP:
        output = (
            "The user chose to skip this step without solving the challenge. Do not "
            "retry it; continue with the rest of the task by another route if one "
            "exists, otherwise report that this step was skipped."
        )
        title = "用户跳过了这一步"
    else:
        output = (
            f"User has taken over and replied: \"{answer or ANSWER_DONE}\". The page "
            f"state was changed by a person, not by your script: first re-open "
            f"{page_hint} and confirm with getAISnapshot() or a screenshot that the "
            "challenge is gone, then continue from there. Do not repeat the action "
            "sequence that triggered the challenge, and mention in your final answer "
            "that the user completed this verification step."
        )
        title = "用户已接管并完成"

    return ToolResult(
        title=title,
        output=output,
        metadata={
            "questions": [question_text],
            "answers": answers,
            "takeover": detail,
        },
    )


DESKTOP_TAKEOVER_DESCRIPTION = """\
Hand the current browser page to the user when a site challenges you with \
something only a person should do: a slider or puzzle captcha, "click the \
characters" checks, a code to type, an SMS/email one-time code, or a \
risk-control page ("security verification", "abnormal environment", "too many \
requests", Cloudflare/GeeTest/Aliyun/Tencent challenge frames).

Do NOT try to solve, drag, guess or bypass these — no coordinate clicks with \
`computer`, no user-agent or cookie tricks, no reloading in a loop. Reload the \
page at most once to confirm the challenge is real, then call this tool.

It suspends the run and shows the user a takeover card. When the page is on \
the cloud desktop the card opens the desktop with input control on; when it is \
in the user's own browser it tells them to finish it there. The run resumes \
when they answer. On "我已完成，继续" re-open the same dev-browser page and \
re-check its state before continuing. A "Rejected" result means the user \
dismissed the card: stop the browser task and say why.

Not for ordinary login forms without a human check — handle those with \
`browser_mode` / `question` instead."""

desktop_takeover_tool = define_tool(
    "desktop_takeover",
    description=DESKTOP_TAKEOVER_DESCRIPTION,
    parameters=DesktopTakeoverArgs,
    execute=execute,
    sandbox_required=False,
    parallel_safe=False,
)
