"""A merchant's physical store (门店), one per workspace in this phase.

`platform_bindings` mirrors what the cloud-desktop login probes learned about
the store on each platform (来客 account, 经营宝 shop id); `data_sources` is
reserved for the 九数云 ingest connections (M2). `persona_status` tracks the
creator-persona bootstrap: none → proposed (bundle card pending) → active.
"""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType

CATEGORIES = ("food", "beauty", "retail", "other")
#: Categories the product actually supports end to end today.
OPEN_CATEGORIES = ("food",)
PERSONA_STATUSES = ("none", "proposed", "active")
PLATFORMS = ("douyin_laike", "meituan_merchant")


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'food'"))
    #: Which platforms the merchant said they run on: subset of PLATFORMS.
    main_platforms: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(64), nullable=True)
    platform_bindings: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    data_sources: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    persona_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'none'"))
    #: Session that hosts the latest persona bootstrap run (the summary card lives there).
    persona_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    persona_started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_stores_workspace"),
        Index("ix_stores_user", "user_id"),
    )
