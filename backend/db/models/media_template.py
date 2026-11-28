"""Versioned recipes and evidence from the guided production that tested them."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType

ENTRY_CHECK = "entry_id in ('video', 'footage_edit', 'menu', 'poster')"


class MediaTemplate(Base):
    __tablename__ = "media_templates"
    __table_args__ = (CheckConstraint(ENTRY_CHECK, name="ck_media_template_entry"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    entry_id: Mapped[str] = mapped_column(String(32))


class MediaTemplateEdition(Base):
    __tablename__ = "media_template_editions"
    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_media_template_version"),
        CheckConstraint("status in ('draft', 'pending', 'listed', 'rejected', 'superseded', 'withdrawn')",
                        name="ck_media_template_status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    template_id: Mapped[str] = mapped_column(ForeignKey("media_templates.id"))
    version: Mapped[int] = mapped_column(Integer)
    source_session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    source_asset_id: Mapped[str] = mapped_column(ForeignKey("file_assets.id"))
    title: Mapped[str] = mapped_column(String(120))
    recipe: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    scene: Mapped[str | None] = mapped_column(String(32))
    preview_key: Mapped[str | None] = mapped_column(String(512))
    preview_mime: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaTemplateTrial(Base):
    __tablename__ = "media_template_trials"
    __table_args__ = (UniqueConstraint("session_id", "edition_id", name="uq_media_template_trial_edition"),)

    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    edition_id: Mapped[str] = mapped_column(ForeignKey("media_template_editions.id"), index=True)
    output_asset_id: Mapped[str | None] = mapped_column(ForeignKey("file_assets.id"))
    snapshot: Mapped[dict | None] = mapped_column(JSONType)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaTemplateReview(Base):
    __tablename__ = "media_template_reviews"
    __table_args__ = (
        ForeignKeyConstraint(
            ["trial_session_id", "edition_id"], ["media_template_trials.session_id", "media_template_trials.edition_id"],
            name="fk_media_template_reviews_trial",
        ),
        CheckConstraint("decision in ('listed', 'rejected', 'withdrawn')", name="ck_media_template_decision"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    edition_id: Mapped[str] = mapped_column(ForeignKey("media_template_editions.id"), index=True)
    trial_session_id: Mapped[str] = mapped_column(String(64))
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MediaTemplateSubmission(Base):
    __tablename__ = "media_template_submissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["trial_session_id", "edition_id"], ["media_template_trials.session_id", "media_template_trials.edition_id"],
            name="fk_media_template_submissions_trial",
        ),
    )

    edition_id: Mapped[str] = mapped_column(ForeignKey("media_template_editions.id"), primary_key=True)
    trial_session_id: Mapped[str] = mapped_column(String(64), unique=True)


class MediaTemplateImport(Base):
    """Stable links to historical records, which continue to own their original data."""

    __tablename__ = "media_template_imports"
    __table_args__ = (CheckConstraint(
        "(example_id is null) <> (video_edition_id is null)", name="ck_media_template_import_source",
    ),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    example_id: Mapped[str | None] = mapped_column(ForeignKey("light_examples.id", ondelete="CASCADE"), unique=True)
    video_edition_id: Mapped[str | None] = mapped_column(
        ForeignKey("video_template_editions.id", ondelete="CASCADE"), unique=True,
    )
