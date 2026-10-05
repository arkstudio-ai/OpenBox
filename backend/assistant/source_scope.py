"""Current SQL scope facts for source reads; never a reusable authority proof.

These joins combine reads in the caller's current transaction. They acquire no
write locks and retain no result between calls, snapshots or provider attempts.
"""
from sqlalchemy import and_, inspect, join, literal, or_, select, tuple_
from sqlalchemy.orm import aliased
from sqlalchemy.orm.attributes import NO_VALUE
from sqlalchemy.orm.util import identity_key

from assistant.policy import AssistantError
from db.models.assistant import AssistantTask
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from session.policy import active_membership


def main_scope(main, *, user_id, workspace_id, main_id):
    return and_(
        main.id == main_id, main.user_id == user_id, main.workspace_id == workspace_id,
        main.is_deleted.is_(False), main.kind == "assistant", main.visibility == "private",
        main.memory_policy == "assistant_isolated",
    )


def authority_columns(*, user_id, workspace_id, main_id):
    main = aliased(Session)
    current_main = select(main.id).where(main_scope(main, user_id=user_id,
        workspace_id=workspace_id, main_id=main_id)).correlate_except(main).scalar_subquery()
    return (active_membership(user_id, workspace_id).label("active_member"),
            current_main.label("authority_main_id"))


def _held_main_policy_allows(db, main_id):
    held = db.sync_session.identity_map.get(identity_key(Session, main_id))
    policy = inspect(held).attrs.memory_policy.loaded_value if held is not None else NO_VALUE
    return policy is NO_VALUE or policy == "assistant_isolated"


def require_authority(db, *, main_id, active_member, authority_main_id):
    # Match _authority's membership -> private-main refusal order. Scalar SQL
    # facts cannot refresh a caller's unflushed main title/model. A held local
    # policy that _authority would reject can only add a refusal, never grant.
    if not active_member:
        raise AssistantError(403, "ASSISTANT_WORKSPACE_FORBIDDEN", "Active workspace membership is required")
    if authority_main_id is None or not _held_main_policy_allows(db, main_id):
        raise AssistantError(404, "ASSISTANT_UNAVAILABLE", "The private assistant is unavailable")


def execution_scope(*, user_id, workspace_id):
    return and_(Session.id == AssistantTask.execution_session_id, Session.user_id == user_id,
        Session.workspace_id == workspace_id, Session.project_id == AssistantTask.project_id,
        Session.is_deleted.is_(False), Session.visibility == "private",
        Session.memory_policy == "assistant_isolated", Session.kind == "normal")


def project_scope(*, user_id, workspace_id):
    return and_(Project.id == AssistantTask.project_id, Project.user_id == user_id,
        Project.workspace_id == workspace_id, Project.is_deleted.is_(False))


def _task_query(*, user_id, workspace_id, main_id, task_id):
    anchor = select(literal(1).label("one")).subquery()
    lookup_id = task_id if isinstance(task_id, str) and task_id else None
    authority = authority_columns(user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    query = (select(*authority,
        AssistantTask, Session, Project.id.label("owned_project_id"),
    ).select_from(anchor).outerjoin(AssistantTask, and_(
        AssistantTask.id == lookup_id, AssistantTask.user_id == user_id,
        AssistantTask.workspace_id == workspace_id, AssistantTask.assistant_session_id == main_id,
    )).outerjoin(Session, execution_scope(user_id=user_id, workspace_id=workspace_id))
    .outerjoin(Project, project_scope(user_id=user_id, workspace_id=workspace_id))
    .execution_options(populate_existing=True))
    return query, authority


def _checked_task(db, row, main_id):
    require_authority(db, main_id=main_id, active_member=row.active_member,
                      authority_main_id=row.authority_main_id)
    if row.AssistantTask is None:
        raise AssistantError(404, "ASSISTANT_TASK_UNAVAILABLE", "Task is unavailable")
    if row.Session is None:
        raise AssistantError(409, "ASSISTANT_EXECUTION_UNAVAILABLE", "The original execution Session is unavailable")
    if row.owned_project_id is None:
        raise AssistantError(404, "ASSISTANT_PROJECT_UNAVAILABLE", "The owned project is unavailable")
    return row.AssistantTask, row.Session


async def read_authorized_task(db, *, user_id, workspace_id, main_id, task_id):
    """Combine _authority followed by the unlocked task scope read, in order."""
    query, _ = _task_query(user_id=user_id, workspace_id=workspace_id, main_id=main_id, task_id=task_id)
    return _checked_task(db, (await db.execute(query)).one(), main_id)


async def read_authorized_task_parts(db, *, user_id, workspace_id, main_id, task_id, keys):
    """Read current scope and one bounded original-part batch in one statement.

    The anchor retains missing scope rows for the original refusal order. No
    Part is selected when current SQL scope or a held local main policy denies
    access. Callers still compare every reference/hash, in its original order.
    """
    query, (active, current_main) = _task_query(user_id=user_id, workspace_id=workspace_id,
        main_id=main_id, task_id=task_id)
    original = join(Part, Message, and_(Message.id == Part.message_id,
        Message.session_id == Part.session_id, Message.user_id == user_id))
    query = query.add_columns(Part).outerjoin(original, and_(
        active.is_(True), current_main.is_not(None), _held_main_policy_allows(db, main_id),
        Session.id.is_not(None), Project.id.is_not(None),
        tuple_(Part.id, Part.message_id, Part.session_id).in_(keys), Part.user_id == user_id,
        or_(Part.session_id == main_id, Part.session_id == Session.id),
    ))
    rows = (await db.execute(query)).all()
    task, execution = _checked_task(db, rows[0], main_id)
    return task, execution, [row.Part for row in rows if row.Part is not None]
