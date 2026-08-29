"""User-scoped creator memory records (ported from bossip's Memory model).

Statuses / scopes / owners / types are deliberately plain strings, not DB
enums — the bossip schema kept them VarChar so new types can land without a
migration, and we keep that discipline here.
"""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class UserMemory(Base):
    __tablename__ = "user_memories"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    # bossip workspaceId maps onto OpenBox projects; NULL = user-global.
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # SHORT_TERM | LONG_TERM
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Convention: writers put {"summary": "..."} in value.
    value: Mapped[dict] = mapped_column(JSONType, default=dict)
    evidence: Mapped[dict] = mapped_column(JSONType, default=dict)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("50"))
    ttl: Mapped[datetime | None] = mapped_column(nullable=True)
    # USER_CONFIRMED | SYSTEM_INFERRED | OPERATOR_CONFIRMED
    owner: Mapped[str] = mapped_column(String(24), nullable=False)
    # CANDIDATE | ACTIVE | EXPIRED | DEPRECATED
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'CANDIDATE'"))
    promoted_from: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_hit_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_user_memories_user_scope_status", "user_id", "scope", "status"),
        Index("ix_user_memories_user_type_status", "user_id", "type", "status"),
        Index("ix_user_memories_ttl", "ttl"),
    )
