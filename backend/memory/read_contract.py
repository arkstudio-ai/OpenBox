"""Read capability shared by legacy tools and the bounded packet adapters."""

# How much each policy lets a turn read (permits below). no_history keeps live
# task state and documents_only uploaded documents: neither covers the other.
READ_WIDTH = {"current_only": 0, "uncertain": 1, "no_history": 2, "documents_only": 2,
              "action_only": 3, "allowed": 3}


def narrowest(*policies):
    """The policy permitting only what every given decision permits; None when none is given.

    Two decisions about the same words (a voice turn and the assistant's own
    judgement, a lost session and its replacement) combine this way: a later
    decision may narrow an earlier restriction, never lift or loosen it.
    """
    known = {policy if policy in READ_WIDTH else "uncertain" for policy in policies if policy}
    if not known:
        return None
    least = min(READ_WIDTH[policy] for policy in known)
    tied = {policy for policy in known if READ_WIDTH[policy] == least}
    if len(tied) == 1:
        return tied.pop()
    # Equal width, different reads: only the current input satisfies both
    # restrictions; action_only also limits execution.
    return "action_only" if tied == {"action_only", "allowed"} else "current_only"


def permits(turn, kind, *, asset_ids=(), bounded=False):
    if turn is None:
        return False
    return kind == "settings" or turn.explicit_read or turn.read_policy == "allowed" or (
        turn.read_policy == "documents_only" and bounded and kind in {"documents", "sources", "search"}) or (
        turn.read_policy == "no_history" and kind == "task_state") or (
        kind in {"assets.describe", "assets.read"}
        and turn.read_policy in {"current_only", "no_history", "uncertain"}
        and bool(asset_ids) and set(asset_ids).issubset(getattr(turn, "current_asset_ids", ())))


def check(ctx, kind, *, consume=False, asset_ids=()):
    from core.config import get_config
    if not get_config().memory.enabled("semantic_recall", ctx.user_id):
        return None
    turn = getattr(ctx, "memory_turn", None)
    if not permits(turn, kind, asset_ids=asset_ids):
        denied = {"status": "restricted", "items": [], "reason": "turn_read_scope"}
        if getattr(turn, "deferred_read", False) and getattr(turn, "read_policy", None) == "uncertain":
            # Not a refusal yet: memory.prepare decides this turn's scope. A
            # call made in the same step as it once read as "no pending cards"
            # and the assistant submitted the same task a second time.
            denied.update(checked=False, read_scope="pending", instruction=(
                "No read was performed: this turn's read scope is decided by memory.prepare. Call it first, "
                "in its own step, then repeat this read. An empty list here is not evidence that nothing "
                "exists; never create, submit or answer anything on that basis."))
        return denied
    if consume and kind != "task_state":
        budget = getattr(ctx, "memory_read_budget", None)
        if budget is None or budget.batches and not budget.legacy_reads or budget.legacy_reads >= 2:
            return {"status": "budget_exhausted", "items": [], "reason": "turn_read_budget"}
        budget.batches = 1
        budget.legacy_reads += 1
    return None


async def current_input_assets(db, turn):
    """Only the current authenticated human Message can grant attachment reads.

    File Parts have no human-origin field. Their containing Message must have
    human text provenance in both canonical events and the current SQL view.
    A model query, a synthetic user role or a stale SQL-only Part grants nothing.
    """
    from sqlalchemy import select
    from assets.service import owned_ready_query
    from db.models.file_asset import FileAsset
    from db.models.message import Message
    from db.models.part import Part
    from db.models.session import Session
    from memory.readers import authority
    from session.agent_event_log import AgentEventProjectionError, load_event_fold_locked
    if not turn.session_id or not turn.turn_id:
        return ()
    rows = list((await db.scalars(select(Part).where(Part.user_id == turn.user_id,
        Part.session_id == turn.session_id, Part.message_id == turn.turn_id, Part.type.in_(("text", "file"))))).all())
    if not any(row.type == "file" for row in rows):
        return ()
    await authority(db, turn)
    session = await db.get(Session, turn.session_id)
    message = await db.get(Message, turn.turn_id)
    if (message is None or message.role != "user" or message.session_id != turn.session_id
            or message.user_id != turn.user_id):
        return ()
    try:
        fold = await load_event_fold_locked(db, session)
        users = [message for message in fold.public_messages() if message.get("role") == "user"]
    except AgentEventProjectionError:
        return ()
    if not users or users[-1]["id"] != turn.turn_id:
        return ()
    canonical = {part["id"]: part.get("data", part) for part in users[-1].get("parts", [])}

    def human(data):
        return (data.get("type") == "text" and data.get("origin") == "human"
            and (data.get("origin_ref") or {}).get("actor_user_id") == turn.user_id
            and not data.get("synthetic") and not data.get("ignored"))

    if not any(row.type == "text" and human(row.data or {}) and human(canonical.get(row.id, {})) for row in rows):
        return ()
    files = {row.data["asset_id"] for row in rows if row.type == "file" and isinstance(row.data, dict)
        and isinstance(row.data.get("asset_id"), str) and row.data["asset_id"]
        and canonical.get(row.id, {}).get("type") == "file"
        and canonical[row.id].get("asset_id") == row.data["asset_id"]}
    if not files:
        return ()
    return tuple(sorted((await db.scalars(owned_ready_query(turn.user_id, turn.workspace_id)
        .with_only_columns(FileAsset.id).where(FileAsset.id.in_(files)))).all()))


async def check_asset_read(ctx, operation, arguments):
    """Narrow current-input capability; each use rechecks SQL and event evidence."""
    from core.config import get_config
    if not get_config().memory.enabled("semantic_recall", ctx.user_id):
        return None
    denied = {"status": "restricted", "items": [], "reason": "turn_read_scope"}
    if operation not in {"assets.describe", "assets.read"} or not isinstance(arguments, dict):
        return denied
    one, many = arguments.get("asset_id"), arguments.get("asset_ids")
    if bool(one) == bool(many):
        return denied
    ids = [one] if one else many
    if (not isinstance(ids, (list, tuple)) or not ids
            or any(not isinstance(value, str) or not 1 <= len(value) <= 64 for value in ids)):
        return denied
    if result := check(ctx, operation, asset_ids=ids):
        return result
    turn = getattr(ctx, "memory_turn", None)
    if permits(turn, "assets"):
        return None
    if (turn.user_id, turn.workspace_id, turn.session_id) != (ctx.user_id, ctx.workspace_id, ctx.session_id):
        return denied
    from db.base import get_db_session
    try:
        async with get_db_session() as db:
            live = await current_input_assets(db, turn)
        return None if set(ids).issubset(live) else denied
    except Exception:
        return denied
