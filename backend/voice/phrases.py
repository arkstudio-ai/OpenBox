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


def greeting_instructions(lang, recap=True, *, first_call=False, unified=False):
    if unified:
        # A previous call's recap can predate a result delivered in text. Only
        # the main assistant may reconcile business state across channels.
        return ("只作一句简短问候，使用一次用户的当前称呼，表示已经接通、正在听。不要回顾旧通话，不提任务、素材、进度或结果。"
                "用户开口就停止问候；业务回答等待主助手的最新结果。") if _lang(lang) == "zh" else (
            "Give one brief greeting using the user's current form of address once, to say you are connected and listening. Do not recap past calls "
            "or mention tasks, assets, progress or results. Stop when the user speaks; business answers "
            "must wait for the main assistant's fresh result.")
    if first_call:
        from assistant.secretary import FIRST_CONTACT
        return FIRST_CONTACT + (" Use the user's current form of address once in this opening only; do not repeat it in subsequent replies. Use English for the introduction and conversation." if _lang(lang) == "en" else
                                "接通时使用一次用户的当前称呼，后续自然交流，不重复称呼。")
    return ("这是已有使用记录的用户接通。按实际聊天上下文和用户偏好简短接话，"
            "不重新介绍身份、职责或能力，不展开业务菜单；详细回答偏好不要求详细开场。" +
            "只作一句接通问候，使用一次用户的当前称呼，表示正在听，不主动提任务、素材、进度、结果或待答卡片。" +
            "用户已开口就优先回应，不补播开场。接通事件不是业务请求。") if _lang(lang) == "zh" else (
        "This is a returning user. Open briefly from the actual conversation and preferences, without an identity, "
        "role or capabilities introduction or a business menu. A preference for detail does not mean a long opening. " +
        "Give one connected-and-listening greeting using the user's current form of address once; do not mention tasks, assets, progress, results or pending cards. " +
        "Give priority to the user's words, without resuming an interrupted opening. Connection is not a business request.")


def note_body(status, user_text, speech, lang, *, report_title=None, asked="", running=()):
    return json.dumps({"source": "background_result", "status": status,
        "request": asked or user_text, "task": report_title, "facts": speech,
        "running_tasks": list(running),
        "state_note": "任务仍在执行，尚未取得成品；不能把计划或交办回执说成完成" if status == "running" else
                      "ok 仅表示这条回复已生成；仅依据 facts 中的实际结果判断任务完成情况，保留将来时与条件"}, ensure_ascii=False)


def joined_note(bodies, lang):
    return NOTE_PREFIX[_lang(lang)] + json.dumps({"purpose": "deliver_results", "results": [json.loads(body) for body in bodies]}, ensure_ascii=False)


def note_text(status, user_text, speech, lang):
    return joined_note([note_body(status, user_text, speech, lang)], lang)


def _delivery(lang):
    return ("向用户传达刚收到的后台事件，讲清是哪件事及其实际状态。自然转述，细节按需要展开；"
            "名称、数字、决定与事实一致。接收请求或记录决定不代表执行完成，未知结果不能说成功。"
            "把已交办的修改与已产出的结果分清：将换图、加字幕等要求送达，不能转述成图片已换好、字幕已加好。"
            "只讲本次事件，不从历史背景追加其他任务、旧卡片、待确认事项或业务菜单。"
            "复述用户说过的信息不等于已写入长期记忆，不据此声称已保存。"
            "这个事件不是用户的新指令，不据此发起或重复业务操作。") if _lang(lang) == "zh" else (
        "Deliver the background event just received, identifying the task and its actual state. "
        "Speak naturally with relevant detail, preserving names, numbers, decisions and facts. "
        "Acceptance or a recorded decision is not execution completion; unknown is not success. "
        "Distinguish a submitted edit from its output: a request to replace an image or add subtitles does not mean either edit is done. "
        "Stay within this event; do not append old tasks, cards or a business menu from history. "
        "Repeating a user's words does not prove they were saved to long-term memory. "
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
        "选任务 AI 时结合需求解释；选视频只说当前可用的业务档次、相关规格和真实费用，不念具体模型或厂商名称，内部保留准确映射。"
        "先给用户容易理解的比较，想了解某项时再展开；不编造质量或速度优势。当前还没有用户回答。" if _lang(lang) == "zh" else
        "Identify the task named on the card, explain the decision and impact, then continue the conversation naturally without another greeting. "
        "Explain choices conversationally instead of reading numbered options or technical identifiers; lead with the current decision and expand when asked. "
        "For task AI choices, explain relevant capabilities. For video choices, use available business tiers, relevant specs and actual costs; never read model or vendor names aloud, and preserve exact routing internally. "
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
    return ("结合对话回答刚收到的工具回执。decision_recorded 仅说明选择已记录，accepted/received 仅说明请求已接收，不代表排队；"
            "execution_confirmed=false 或 execution_result=null 时，业务执行结果尚未返回，不能说成已发出、已创建或已完成。"
            "invalid_arguments、conflict、target_mismatch、need_user_answer、unavailable 都不是成功，this_call_applied=false 时不能说已提交或已记录；"
            "按回执的 conversation 说明实际提交的任务，不能把一个任务的成功说成另一任务成功。"
            "current_login_state=unknown 表示当前登录状态没有核验，明确说还无法判断是否连接；历史记录缺失不证明已断开。"
            "仅参数格式错误可保持同一用户决定校正参数，结果未知须先查询。retry_allowed=false 时停止调用，说明未提交并等用户下一步。"
            "按实际状态自然回应；需要的选择已明确就不重复确认。") if _lang(lang) == "zh" else (
        "Answer the tool receipt in conversation context. decision_recorded means only a recorded choice; "
        "accepted/received means only a received request, not a queue. execution_confirmed=false or execution_result=null means "
        "no execution result is available; do not describe the operation as sent, created or complete. "
        "invalid_arguments, conflict, target_mismatch, need_user_answer and unavailable are failures; this_call_applied=false cannot be described as submitted or recorded. "
        "Name the actual conversation in the receipt; success on one task is not success on another. "
        "current_login_state=unknown means live login was not checked: say it is not yet known whether connected; missing history is not evidence of disconnection. "
        "Correct a rejected argument shape while preserving the user's exact choice; query first when the outcome is unknown. "
        "When retry_allowed=false, stop calling tools, explain that nothing was submitted and wait for the user. "
        "Respond naturally to its actual state, without asking again for a clear decision.")


def notice_instructions(lang):
    return _delivery(lang)


def progress_instructions(step, lang):
    return _delivery(lang) + ("这是一次等待提示，只用一句简短的话说明当前阶段。不要重复用户需求、图片用途、讲稿、字幕等已知选择，"
        "不要再次询问确认，不预告尚未发生的制作或完成通知。当前事实：" if _lang(lang) == "zh" else
        "This is a waiting update. Use one short sentence about the current stage, without repeating requirements, "
        "assets, script or subtitle choices. Do not ask for confirmation again or promise later production or completion notices. Current facts: ") + (step or
        ("请求仍在处理中，尚无结果或完成时间。" if _lang(lang) == "zh" else "Still processing; no result or ETA yet."))


def receipt_instructions(lang, *, delayed=False):
    """Transport facts only: receiving speech does not start the named task."""
    if _lang(lang) == "zh":
        purpose = "等待时间较长，简短说明回复仍未返回。" if delayed else "简短回应，确认已经听到用户刚才说的话。"
        return ("这是服务端明确要求的通话状态播报，不是业务回答。" + purpose +
                "已核实的事实只有：原话已保存并交给主助手，正在等待回复；用户可以继续说话。"
                "用一句话自然表达，不提工具、转交或内部流程，不回答原话中的业务问题，不复述旧任务或待办。"
                "排队的发言不等于任务已开始，不推断业务操作或扣费已经发生，不编造进度和完成时间。")
    purpose = "Briefly explain that the reply is still pending. " if delayed else "Briefly acknowledge hearing the user's latest words. "
    return ("The server explicitly requests a call-status announcement, not a business answer. " + purpose +
            "The only verified facts: the original words were saved and sent to the main assistant; its reply "
            "is pending and the user can keep speaking. Say this naturally in one sentence, without mentioning "
            "tools, handoffs or internal processes. Do not answer the business question or repeat old tasks. "
            "Queued speech does not prove a task started. Do not infer business operations, charges, "
            "execution progress or an ETA.")


async def user_language(user_id):
    from db.repository.preference_repo import PgPreferenceRepo
    try:
        locale = str(((await PgPreferenceRepo().get(user_id)) or {}).get("extra", {}).get("locale") or "")
    except Exception:
        return "zh"
    return "en" if locale.lower().startswith("en") else "zh"
