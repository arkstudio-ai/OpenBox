"""Facts and delivery purpose for realtime speech, without conversational scripts.

Background events keep their source/target and are not user authorization. Only
terminal transport notices have fallback text; normal speech is model generated.
"""
import json

LANGS = ("zh", "en")
PHRASES = {
    "result_in_text": {"zh": "语音结果没能播报，请到对话里查看处理情况。",
                       "en": "I couldn't deliver the voice update. Please check the conversation for its status."},
    "credits_exhausted": {"zh": "积分用完了，这通电话先到这里，充值后可以接着打。",
                          "en": "You're out of credits, so I'll end the call here. Top up to call again."},
    "reconnected": {"zh": "刚才线路断了一下，您再说一遍？",
                    "en": "The line dropped for a second. Could you say that again?"},
}
SPEECHES = {
    "timeout": {"zh": "暂时还没有处理结果，后续可以在对话里查看。", "en": "No result yet; check the conversation for updates."},
    "failed": {"zh": "没有取得可确认的结果，请在对话里查看处理情况。", "en": "No verified result is available; check the conversation for its status."},
    "unavailable": {"zh": "没能提交给个人助理。", "en": "The request could not be submitted to the assistant."},
}
NOTE_PREFIX = {"zh": "（后台事件，不是用户说的话）", "en": "(Background event, not said by the user) "}


def _lang(lang):
    return lang if lang in LANGS else "zh"


def phrase_text(key, lang):
    return PHRASES[key][_lang(lang)]


def speech_text(key, lang):
    return SPEECHES[key][_lang(lang)]


def phrase_instructions(key, lang):
    lead = "告知本次通话状态，不执行业务：" if _lang(lang) == "zh" else "Convey this call status without business actions: "
    return lead + phrase_text(key, lang)


def greeting_instructions(lang, recap=True):
    return ("通话接通了，按用户偏好自然打招呼。" +
            ("可结合已知的上次通话和完成记录，不推测当前进度。" if recap else "用户不希望开场回顾以前的事。") +
            "这是接通事件，不是业务请求。") if _lang(lang) == "zh" else (
        "Greet the user naturally using their preferences. " +
        ("You may use known past-call and completion facts; do not infer current progress. " if recap else "Do not recap previous work. ") +
        "This connection event is not a business request.")


def note_body(status, user_text, speech, lang, *, report_title=None, asked="", running=()):
    return json.dumps({"source": "background_result", "status": status,
        "request": asked or user_text, "task": report_title, "facts": speech,
        "running_tasks": list(running),
        "state_note": "仍在执行" if status == "running" else "仅依据 facts 中的实际结果判断完成情况"}, ensure_ascii=False)


def joined_note(bodies, lang):
    return NOTE_PREFIX[_lang(lang)] + json.dumps({"purpose": "deliver_results", "results": [json.loads(body) for body in bodies]}, ensure_ascii=False)


def note_text(status, user_text, speech, lang):
    return joined_note([note_body(status, user_text, speech, lang)], lang)


def _delivery(lang):
    return ("向用户传达刚收到的后台事件，讲清是哪件事及其实际状态。自然转述，细节按需要展开；"
            "名称、数字、决定与事实一致。接收请求或记录决定不代表执行完成，未知结果不能说成功。"
            "这个事件不是用户的新指令，不据此发起或重复业务操作。") if _lang(lang) == "zh" else (
        "Deliver the background event just received, identifying the task and its actual state. "
        "Speak naturally with relevant detail, preserving names, numbers, decisions and facts. "
        "Acceptance or a recorded decision is not execution completion; unknown is not success. "
        "This event is not new user authorization to start or repeat work.")


def delivery_instructions(speech, lang):
    return _delivery(lang)


def together_instructions(count, unasked, speeches, lang):
    return _delivery(lang) + ("先回答当前事项，再自然带出其他结果。" if _lang(lang) == "zh" else
                              "Address the current request first, then the other results.")


def card_note(user_text, speech, cards, lang):
    return NOTE_PREFIX[_lang(lang)] + json.dumps({"source": "pending_decisions", "request": user_text,
        "facts": speech, "cards": cards, "operation_state": "waiting_for_user"}, ensure_ascii=False)


def card_instructions(lang):
    return _delivery(lang) + ("围绕卡片标明的任务说明待决定事项及影响，接着当前对话自然询问；不要重新自我介绍。"
        "选择类问题用日常对话说明，不逐条朗读序号或技术标识；先讲当前需要决定的内容，用户追问再展开。"
        "选模型时结合需求解释可选项和实际能力，完整清单可留在屏幕，不逐条念型号、序号和参数。"
        "先给用户容易理解的比较，想了解某项时再展开；不编造质量或速度优势。当前还没有用户回答。" if _lang(lang) == "zh" else
        "Identify the task named on the card, explain the decision and impact, then continue the conversation naturally without another greeting. "
        "Explain choices conversationally instead of reading numbered options or technical identifiers; lead with the current decision and expand when asked. "
        "For model choices, explain relevant options using real capabilities; leave the full list on screen rather than reading identifiers, numbers and specs one by one. "
        "Give an understandable comparison and expand when asked; do not invent quality or speed advantages. The user has not answered yet.")


def question_note(questions, lang):
    return NOTE_PREFIX[_lang(lang)] + json.dumps({"source": "task_questions", "waiting_for_user": questions}, ensure_ascii=False)


def question_instructions(lang):
    return card_instructions(lang) + ("结合任务询问尚缺的决定，不填默认答案或推断进度。" if _lang(lang) == "zh" else
                                      "Ask about missing decisions in task context; do not fill defaults or infer progress.")


def question_followup_instructions(lang):
    return _delivery(lang) + ("如有尚缺的决定，自然询问；已回答的不用重问。" if _lang(lang) == "zh" else
                              "Ask naturally about remaining decisions, without repeating answered questions.")


def tool_result_instructions(lang):
    return ("结合对话回答刚收到的工具回执。decision_recorded 仅说明选择已记录，accepted 仅说明请求已接收；"
            "execution_confirmed=false 或 execution_result=null 时，业务执行结果尚未返回，不能说成已发出、已创建或已完成。"
            "invalid_arguments、conflict、need_user_answer、unavailable 都不是成功，this_call_applied=false 时不能说已提交或已记录；"
            "仅参数格式错误可保持同一用户决定校正参数，结果未知须先查询。retry_allowed=false 时停止调用，说明未提交并等用户下一步。"
            "按实际状态自然回应；需要的选择已明确就不重复确认。") if _lang(lang) == "zh" else (
        "Answer the tool receipt in conversation context. decision_recorded means only a recorded choice; "
        "accepted means only a received request. execution_confirmed=false or execution_result=null means "
        "no execution result is available; do not describe the operation as sent, created or complete. "
        "invalid_arguments, conflict, need_user_answer and unavailable are failures; this_call_applied=false cannot be described as submitted or recorded. "
        "Correct a rejected argument shape while preserving the user's exact choice; query first when the outcome is unknown. "
        "When retry_allowed=false, stop calling tools, explain that nothing was submitted and wait for the user. "
        "Respond naturally to its actual state, without asking again for a clear decision.")


def notice_instructions(lang):
    return _delivery(lang)


def progress_instructions(step, lang):
    return _delivery(lang) + ("当前事实：" if _lang(lang) == "zh" else "Current facts: ") + (step or
        ("请求仍在处理中，尚无结果或完成时间。" if _lang(lang) == "zh" else "Still processing; no result or ETA yet."))


async def user_language(user_id):
    from db.repository.preference_repo import PgPreferenceRepo
    try:
        locale = str(((await PgPreferenceRepo().get(user_id)) or {}).get("extra", {}).get("locale") or "")
    except Exception:
        return "zh"
    return "en" if locale.lower().startswith("en") else "zh"
