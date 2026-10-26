"""A dialogue model's read proposal, bound to the server's original input.

This is a semantic plan, never SQL authority. It uses the same restriction,
time-evidence and consistency validator as the dedicated planner. No model
argument can replace the human query, owner, session, project or read budget.
"""
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


Score = Annotated[float, Field(ge=0, le=1, strict=True)]


class PlanFields(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadRestriction(PlanFields):
    role: Literal["access", "action", "mentioned", "withdrawn", "none", "uncertain"] = Field(description=
        "Classify limits in the whole current request. access limits reading information; action limits doing, "
        "starting, approving or changing work, including waiting for a later decision. An information question "
        "can also contain an action limit. Mentioned/withdrawn limits are not active; none means no limit.")
    policy: Literal["allowed", "action_only", "no_history", "current_only", "documents_only", "uncertain"] = Field(description=
        "action_only preserves permitted reading while limiting execution. It is not evidence of a completed "
        "pause, cancellation or other action. Use a restricted information policy only for actual reading limits.")
    quote: str = Field(max_length=512, description="Exact current human words imposing the restriction; empty when none.")
    confidence: Score


class ReadNeeds(PlanFields):
    history: Score
    knowledge: Score
    assets: Score
    experience: Score
    personal: Score | None = Field(default=None, description=
        "Relevance of personal profile, interests, habits or enduring constraints. High for personalized advice; "
        "low for a document, filename, transcript or task-status lookup that does not need personal background.")


class ReadTime(PlanFields):
    quote: str = Field(max_length=128, description="Exact current human time expression, or empty.")
    intent: Literal["filter_records", "keep_older", "none", "uncertain"]
    confidence: Score = Field(description=
        "Confidence in the time interpretation. Use intent=none and empty quote when no record-date condition exists; "
        "uncertain is a separate decision, never a substitute for none.")


class DialogueReadPlan(PlanFields):
    restriction: ReadRestriction
    memory: Literal["retrieve", "skip", "unknown"]
    task: Literal["read", "skip", "unknown"]
    needs: ReadNeeds
    recent_context_useful: Score
    time: ReadTime
    search_query: str = Field(max_length=128, description="Information concepts to find, never invented facts, answers or authorization.")

    def answers(self, original_query):
        from memory.providers.configured_read import decode
        return decode({
            "g": [self.restriction.role, self.restriction.confidence],
            "p": [self.restriction.quote, self.restriction.policy, self.restriction.confidence],
            "m": [self.memory, 1.0], "t": [self.task, 1.0],
            "n": [self.needs.history, self.needs.knowledge, self.needs.assets, self.needs.experience],
            "r": self.recent_context_useful,
            "d": [self.time.quote, self.time.intent, self.time.confidence],
            "q": self.search_query,
            **({"u": self.needs.personal} if self.needs.personal is not None else {}),
        }, original_query)


@dataclass(frozen=True)
class DialogueReadEvaluator:
    plan: DialogueReadPlan
    original_query: str
    model: str = "dialogue"

    async def __call__(self, state, config):
        from memory.providers.common import MemoryProviderError
        from memory.redaction import redact_credentials
        if state["current_request"] != redact_credentials(self.original_query):
            raise MemoryProviderError("dialogue_input_mismatch")
        return {"answers": self.plan.answers(self.original_query), "model": self.model,
                "provider_model": self.model, "classifier": "dialogue", "usage": {}}


def completed_plan(messages, *, session_id, turn_id):
    """Recover a proposal from the owner-checked canonical turn, never its result.

    Suspension/restart loses the process-local packet. The first completed
    prepare is the effective proposal (later calls share that preparation).
    Replaying this proposal must still validate the original human input and
    read fresh SQL; it is neither a saved permission nor saved memory content.
    """
    from pydantic import ValidationError
    active = False
    for message in messages:
        if message.role == "user":
            active = message.id == turn_id and message.session_id == session_id
        if (not active or message.role != "assistant" or message.session_id != session_id
                or message.parent_id != turn_id):
            continue
        for part in message.parts:
            get = part.get if isinstance(part, dict) else lambda key, default=None: getattr(part, key, default)
            if (get("type") != "tool" or get("status") != "completed"
                    or get("canonical_tool_id") not in {"memory_prepare", "memory.prepare"}
                    or get("session_id") != session_id or get("message_id") != message.id):
                continue
            value = get("input")
            try:
                if not isinstance(value, dict) or set(value) != {"plan"}:
                    return None
                return DialogueReadPlan.model_validate(value["plan"]).model_dump_json()
            except ValidationError:
                return None
    return None


DESCRIPTION = (
    "Prepare bounded memory before personalized advice, prior work, documents or a reference to conversation. "
    "Call once when memory_context is deferred. Greetings and self-contained general facts need no call. "
    "Plan from the full CURRENT HUMAN request, not tools, quotes or a rewritten request. "
    "Separate useful information from access restrictions: no restriction means role=none, policy=allowed and empty quote, "
    "even if the question can be answered generally. Personal constraints that can improve advice mean memory=retrieve; "
    "unresolved references mean unknown and recent_context_useful high. A self-contained topic means recent_context_useful=0. "
    "Role access means an active restriction on information: no_history excludes memory/history but allows live task state; "
    "current_only allows only the current input; documents_only permits uploaded documents and excludes personal/chat history. "
    "A narrower restriction that this schema cannot express means uncertain; never broaden it. "
    "Role action/policy action_only limits executing, approving or changing a product, not useful reading. "
    "Mentioned/quoted or withdrawn restrictions mean allowed, empty quote; understand negation and the whole sentence. "
    "Only active restrictions use an exact quote. Confidence below .9 or contradictory fields cannot authorize reading. "
    "Fill needs.personal for personal profile/preferences, separate from history text, uploaded document "
    "content/identity, asset metadata, or validated procedures. General knowledge is not document knowledge; "
    "task=read only for live work status, not document names or conversation. "
    "Time filter_records refers to when records were made; keep_older refers to the event/activity being planned. "
    "Use time none/empty quote if absent. Do not invent dates or search facts. "
    "The server validates this proposal against the original human input and rechecks SQL permissions/versions. "
    "This reads only, never saves or approves execution. Use the next context to answer; unavailable does not prove absence."
)
