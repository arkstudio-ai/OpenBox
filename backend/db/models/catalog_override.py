"""Operator control over catalogue entries that live in code, not in the DB."""
from datetime import datetime

from sqlalchemy import Boolean, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class CatalogOverride(Base):
    """A shelf decision for one ``skill/catalog.py`` entry.

    Catalogue entries ship with the code, so the admin console cannot edit them
    the way it edits a ``user_skills`` row.  It writes an override instead: no
    row means the entry keeps the ``listing`` declared in code, which also
    means a redeploy never resurrects something an operator took down.
    """

    __tablename__ = "catalog_overrides"

    #: ``skill:anthropic-skills`` / ``mcp:firecrawl`` — the same key
    #: ``skill_installs.catalog_id`` uses for catalogue entries.
    catalog_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    #: ``listed`` | ``delisted``.  Catalogue entries never go through review,
    #: so the author-facing states (pending / rejected) do not apply.
    listing: Mapped[str] = mapped_column(String(16), nullable=False)
    featured: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    #: Delist reason, kept for the audit trail; catalogue entries have no
    #: author to notify.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(nullable=False)
