"""Uploaded-asset records: files that moved browser → OSS → cloud desktop.

The bytes never touch the backend — this table is the ledger: who uploaded
what, under which OSS key, and whether the upload actually completed. It is
also the resource centre's index: every row carries the project it belongs to
and whether a person or the agent produced it, which is what the two-level
"project → source" filter reads.
"""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class FileAsset(Base):
    __tablename__ = "file_assets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Project the resource is filed under. Null = unfiled (no project context
    #: at upload time), which the UI shows in its own bucket.
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Object key inside the asset bucket (assets/{user}/{asset}/{name}).
    oss_key: Mapped[str] = mapped_column(String(512), nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False, default="application/octet-stream")
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: pending → the PUT URL was issued; ready → object verified in OSS.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    #: "user" → a person uploaded it; "agent" → the model produced it in the
    #: sandbox and pushed it out (view_image / share_file / screenshots).
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'user'"))
    #: Working bytes the agent needs to see once (desktop screenshots). Kept
    #: for the chat transcript, hidden from the resource centre.
    transient: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    #: Soft delete: the OSS object is removed, the row stays so old messages
    #: still know what used to hang off them.
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_file_assets_user_created", "user_id", "created_at"),
        Index("ix_file_assets_user_project", "user_id", "project_id", "created_at"),
    )
