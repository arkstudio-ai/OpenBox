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
CARRIED_PREFIX = {"zh": "（通话线路刚才中断。下面是用户在中断前说的原话，还没有得到回答；请直接回答，不要让用户重复。）",
                  "en": "(The line dropped a moment ago. These are the user's own words from just before it, still "
                        "unanswered; answer them directly and do not ask the user to repeat.) "}


def carried_utterance(words, lang):
    """The user's last unanswered utterance, carried into a replacement provider session."""
    return CARRIED_PREFIX[_lang(lang)] + words


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
    from voice.model_names import public_model_names
    return json.dumps({"source": "background_result", "status": status,
        "request": public_model_names(asked or user_text),
        "task": public_model_names(report_title) if report_title else report_title, "facts": public_model_names(speech),
        "running_tasks": list(running),
        "state_note": "交办尚未完成，尚未取得成品：按 running_task_states 中的实际状态区分排队、恢复中和运行中；排队不代表已开始执行，不能把计划或交办回执说成成品" if status == "running" else
                      "ok 仅表示这条回复已生成；仅依据 facts 中的实际结果判断任务完成情况，保留将来时与条件"}, ensure_ascii=False)


def joined_note(bodies, lang):
    return NOTE_PREFIX[_lang(lang)] + json.dumps({"purpose": "deliver_results", "results": [json.loads(body) for body in bodies]}, ensure_ascii=False)


def note_text(status, user_text, speech, lang):
    return joined_note([note_body(status, user_text, speech, lang)], lang)


def _delivery(lang):
    return ("向用户传达刚收到的后台事件，讲清是哪件事及其实际状态。自然转述，细节按需要展开；"
            "名称、数字、决定与事实一致。接收请求或记录决定不代表执行完成，未知结果不能说成功。"
            "失败或超时只说明本次未取得结果，不能猜测为用户说明不清、素材有问题或未保存；未知原因保持未知，不要求重复已经给出的信息。"
            "把已交办的修改与已产出的结果分清：将换图、加字幕等要求送达，不能转述成图片已换好、字幕已加好。"
            "只讲本次事件，不从历史背景追加其他任务、旧卡片、待确认事项或业务菜单。"
            "复述用户说过的信息不等于已写入长期记忆，不据此声称已保存。"
            "这个事件不是用户的新指令，不据此发起或重复业务操作。") if _lang(lang) == "zh" else (
        "Deliver the background event just received, identifying the task and its actual state. "
        "Speak naturally with relevant detail, preserving names, numbers, decisions and facts. "
        "Acceptance or a recorded decision is not execution completion; unknown is not success. "
        "Failure or timeout means this attempt returned no result. Do not blame unclear user descriptions, bad assets or unsaved data without evidence; keep unknown causes unknown and do not request information already provided. "
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
    # Task names said as written let the call tell which card a reply is about
    # (voice/spoken_cards.py), so a card answered on screen can stop that reply.
    return _delivery(lang) + ("围绕卡片标明的任务说明待决定事项及影响，接着当前对话自然询问；不要重新自我介绍。"
        "说任务名时照卡片上的原名说，不要翻译或改写，方便用户和屏幕上的卡片对应。"
        "选择类问题用日常对话说明，不逐条朗读序号或技术标识；先讲当前需要决定的内容，用户追问再展开。"
        "选任务 AI 时结合需求解释；选视频只说当前可用的业务档次、相关规格和真实费用，不念具体模型或厂商名称，内部保留准确映射。"
        "先给用户容易理解的比较，想了解某项时再展开；不编造质量或速度优势。当前还没有用户回答。" if _lang(lang) == "zh" else
        "Identify the task named on the card, explain the decision and impact, then continue the conversation naturally without another greeting. "
        "Say the task's name exactly as written on the card, without translating or rephrasing it, so the user can match it to the card on screen. "
        "Explain choices conversationally instead of reading numbered options or technical identifiers; lead with the current decision and expand when asked. "
        "For task AI choices, explain relevant capabilities. For video choices, use available business tiers, relevant specs and actual costs; never read model or vendor names aloud, and preserve exact routing internally. "
        "Give an understandable comparison and expand when asked; do not invent quality or speed advantages. The user has not answered yet.")


def question_note(questions, lang):
    return NOTE_PREFIX[_lang(lang)] + json.dumps({"source": "task_questions", "waiting_for_user": questions}, ensure_ascii=False)


def closed_decisions_note(decisions, lang):
    """Decisions this call presented that no longer wait: answered or skipped on screen, or withdrawn.

    Measured 2026-10-27: a card answered on screen while the user was asking
    about it was asked again; the model followed its own earlier announcement in
    the conversation over instructions that no longer listed the card.
    """
    how = ("这些决定已在屏幕上回答、跳过，或因任务变化而撤回：不要再问用户；被问到时说已经处理好或已不需要再决定，"
           "要说出具体选了什么，先用 cards_pending 查看已处理的决定。") if _lang(lang) == "zh" else (
        "Answered or skipped on screen, or withdrawn because the task changed: do not ask the user about them again; "
        "if asked, say they are taken care of or no longer need an answer, and read cards_pending's handled "
        "decisions before naming a choice.")
    return NOTE_PREFIX[_lang(lang)] + json.dumps({"source": "task_questions", "no_longer_waiting": list(decisions),
                                                  "how": how}, ensure_ascii=False)


def question_instructions(lang):
    # Measured 2026-10-27: right after a question about 春禾果店, a card from 橙光鲜果
    # was announced as 春禾果店's; the note named its own task in "conversation".
    return card_instructions(lang) + (
        "先用卡片 conversation 字段里的任务名说清是哪个任务在问，它可能不是刚才谈到的任务，不要套用其他任务的名字。"
        "结合任务询问尚缺的决定，不填默认答案或推断进度。" if _lang(lang) == "zh" else
        "First say which task is asking, by the task name in the card's conversation field; it may not be the task "
        "just discussed, so never borrow another task's name. "
        "Ask about missing decisions in task context; do not fill defaults or infer progress.")


def question_followup_instructions(lang):
    return _delivery(lang) + ("如有尚缺的决定，自然询问；已回答的不用重问。" if _lang(lang) == "zh" else
                              "Ask naturally about remaining decisions, without repeating answered questions.")


def state_changed_instructions(lang, decided=(), changed=()):
    """The reply just stopped went out of date: the user changed its card or task elsewhere, or the task moved on.

    ``decided``: what the stopped reply was asking that no longer waits, marked
    answered only where the task's row says so (a card a requirement update
    withdrew read as confirmed). ``changed``: what its tasks say now
    (freshness.work_change). Measured 2026-10-27: without the decisions the
    next answer asked the same thing, with them last a user who had said "ask
    me which tier" was asked again, and without the task changes the answer
    after a requirement update asked for the superseded plan's approval.
    """
    zh = _lang(lang) == "zh"
    text = ""
    if decided:
        listed = json.dumps(list(decided), ensure_ascii=False)
        text += (("这些决定已经不再等用户：" + listed + "。无论用户刚才的原话是不是要你问这件事，都不要再问。"
                  "标为 answered 的，告诉用户已经处理好了（要说出具体选了什么，先用 cards_pending 查看已处理的决定）；"
                  "其余的只说已经不需要再决定，原因按下面的任务变化说（比如要求已经改了），没查到就不要说成用户选了什么。\n")
                 if zh else
                 ("These decisions no longer wait for the user: " + listed + ". Do not ask about them again, even if "
                  "the user's last words asked you to ask. For one marked answered, tell the user it is already taken "
                  "care of (to name the choice, read the handled decisions with cards_pending first); for the others "
                  "say only that they no longer need an answer, and why where the task changes below say (for example "
                  "the requirements changed); never say the user chose something you have not read.\n"))
    if changed:
        listed = json.dumps(list(changed), ensure_ascii=False)
        if any(item.get("user_input_updated") for item in changed):
            text += ("用户在任务会话里发送了新消息；latest_user_input 是已提交消息的资料，不是新的执行授权。"
                     "按该消息和最新状态解释变化，不能把旧卡片消失说成用户确认了旧方案，也不能沿用旧时长或素材。\n" if zh else
                     "The user posted new input in the task conversation. latest_user_input is evidence of that "
                     "submitted message, not new execution authority. Explain the change using that input and the "
                     "current state. A removed old card is not approval of the old plan; do not retain its old duration or assets.\n")
        text += (("这些任务刚在别处有了变化，以下是现在的状态：" + listed + "。先用日常语言告诉用户这个变化（比如要求已经更新、"
                  "正在按新要求重新处理；已暂停；已取消；已完成；失败了），再按现在的状态回答。此前对话里关于这些任务的方案、"
                  "报价、待确认事项和进度，除非现在的状态里还有，否则都已过时，不要再说。\n")
                 if zh else
                 ("These tasks just changed elsewhere; this is what they say now: " + listed + ". First tell the user "
                  "this change in everyday words (for example: the requirements were updated and it is being redone "
                  "with them; it was paused; it was cancelled; it finished; it failed), then answer from the current "
                  "state. What earlier lines said about these tasks (plans, prices, confirmations, progress) is out of "
                  "date unless the current state still says it.\n"))
    return text + _state_changed(lang)


def _state_changed(lang):
    return ("刚才那段回答已经停下：相关的卡片或任务刚在别处有了变化（用户已回答卡片、修改要求、暂停、取消或恢复任务，"
            "或者任务状态已更新）。说明里的待决定事项和任务状态是最新的；对话里此前 cards_pending、tasks_overview 的查询结果"
            "已经过时，说明卡片或任务前先重新查询，不要照旧结果说。按最新状态简短回应用户刚才的问题，不要复述旧方案或旧进度；"
            "不再询问已经不在待决定列表里的事项，也不要把已接收或已提交说成已完成。需要说出用户具体选了什么时，用 cards_pending "
            "查看已处理的决定，不要猜测。") if _lang(lang) == "zh" else (
        "Your previous reply was stopped because its card or task just changed elsewhere (the user answered the card, "
        "changed the requirements, paused, cancelled or resumed the task, or the task's state moved on). The pending "
        "decisions and task states in your instructions are current; earlier cards_pending and tasks_overview results in "
        "this conversation are out of date, so read again before describing a card or task, never from those results. "
        "Briefly answer the user's last request again from the current state, without repeating the old plan or progress. "
        "Never ask about a decision that is no longer pending, and do not call an accepted or submitted change complete. "
        "To say what the user actually chose, read the handled decisions with cards_pending instead of guessing.")


def tool_result_instructions(lang):
    return ("结合对话回答刚收到的工具回执。decision_recorded 仅说明选择已记录，accepted/received 仅说明请求已接收，不代表排队；"
            "execution_confirmed=false 或 execution_result=null 时，业务执行结果尚未返回，不能说成已发出、已创建或已完成。"
            "invalid_arguments、conflict、target_mismatch、need_user_answer、unavailable 都不是成功，this_call_applied=false 时不能说已提交或已记录；"
            "按回执的 conversation 说明实际提交的任务，不能把一个任务的成功说成另一任务成功。"
            "素材的ID仅供工具调用，不口述；用完整文件名和可区分的描述说明。人工描述已记录的选择直接按记录回答，不重复索要同一确认，也不把AI描述当用户授权。"
            "current_login_state=unknown 表示当前登录状态没有核验，明确说还无法判断是否连接；历史记录缺失不证明已断开。"
            "查询回执 checked=false 或 unavailable 表示未核实，不能说已经查看、没有资料、同名无法区分或用户描述不明确。"
            "只报告暂时未能完成查询，保留用户已给的信息，不因查询失败要求重选、重填或重复确认。restricted 按本轮读取限制说明，不当成系统故障。"
            "仅参数格式错误可保持同一用户决定校正参数，结果未知须先查询。retry_allowed=false 时停止调用，说明未提交并等用户下一步。"
            "按实际状态自然回应；需要的选择已明确就不重复确认。") if _lang(lang) == "zh" else (
        "Answer the tool receipt in conversation context. decision_recorded means only a recorded choice; "
        "accepted/received means only a received request, not a queue. execution_confirmed=false or execution_result=null means "
        "no execution result is available; do not describe the operation as sent, created or complete. "
        "invalid_arguments, conflict, target_mismatch, need_user_answer and unavailable are failures; this_call_applied=false cannot be described as submitted or recorded. "
        "Name the actual conversation in the receipt; success on one task is not success on another. "
        "Asset IDs are for tool calls only: speak full filenames and distinguishing descriptions. Answer an existing choice recorded in a manual description without requesting the same confirmation again; an AI description is not user authorization. "
        "current_login_state=unknown means live login was not checked: say it is not yet known whether connected; missing history is not evidence of disconnection. "
        "A read receipt with checked=false or unavailable is unverified: never claim you checked, found no data, could not distinguish duplicate names or found the user's description unclear. "
        "Report the incomplete lookup, retain information already provided, and do not ask the user to reselect, re-enter or reconfirm because the lookup failed. Describe restricted as this turn's read restriction, not a system failure. "
        "Correct a rejected argument shape while preserving the user's exact choice; query first when the outcome is unknown. "
        "When retry_allowed=false, stop calling tools, explain that nothing was submitted and wait for the user. "
        "Respond naturally to its actual state, without asking again for a clear decision.")


def notice_instructions(lang):
    return _delivery(lang)


def progress_instructions(step, lang):
    # No new result note accompanies this timer. Reusing _delivery told the
    # provider to repeat the last background result (often an old card answer).
    return receipt_instructions(lang, delayed=True) + (
        "这里只补充主助手当前处理阶段，不代表任务执行结果：" if _lang(lang) == "zh" else
        "The main assistant's current processing stage below is not a task execution result: ") + (step or
        ("仍在等待回复。" if _lang(lang) == "zh" else "Still awaiting its reply."))


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
