"""Bounded execution failures for assistant reads, separate from report delivery errors."""
import json

from sqlalchemy import select, tuple_

from db.models.assistant import AssistantTask
from db.models.message import Message
from memory.redaction import redact_credentials


def execution_error_view(error) -> dict | None:
    if not isinstance(error, dict) or not error:
        return None
    value = {}
    for key, limit in (("code", 128), ("reason_code", 128), ("message", 1600)):
        text = error.get(key)
        if isinstance(text, str) and text.strip():
            value[key] = redact_credentials(text)[:limit]
    if isinstance(error.get("retryable"), bool):
        value["retryable"] = error["retryable"]
    # Older records only kept the safe cause on the nested delivery error.
    nested = error.get("last_error")
    if isinstance(nested, dict):
        for key in ("code", "retryable"):
            item = nested.get(key)
            if key == "retryable" and isinstance(item, bool):
                value.setdefault(key, item)
            elif key == "code" and isinstance(item, str):
                value.setdefault("reason_code", redact_credentials(item)[:128])
    return value or None


def execution_error_text(error) -> str | None:
    value = execution_error_view(error)
    return "Execution error (recorded by the task): " + json.dumps(
        value, ensure_ascii=False) if value else None


async def result_execution_error(db, result, *, user_id: str) -> dict | None:
    """Caller must first authorize the result and its execution session."""
    return (await result_execution_errors(db, [result], user_id=user_id)).get(result.id)


async def result_execution_errors(db, results, *, user_id: str) -> dict:
    selected = {(row.task_id, row.result_message_id): row.id for row in results
                if row is not None and row.result_message_id}
    if not selected:
        return {}
    rows = await db.execute(select(AssistantTask.id, Message.id, Message.error)
        .join(Message, Message.session_id == AssistantTask.execution_session_id)
        .where(AssistantTask.user_id == user_id, Message.user_id == user_id,
               Message.role == "assistant",
               tuple_(AssistantTask.id, Message.id).in_(selected)))
    return {selected[(task_id, message_id)]: execution_error_view(error)
            for task_id, message_id, error in rows}
