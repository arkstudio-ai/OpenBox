"""Provenance for store skills and MCP servers installed by a user."""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


def _derive_catalog_id(context) -> str:
    """Fill ``catalog_id`` for a community install from its ``user_skills`` row.

    Community installs already carry the only identifier they can have, so
    asking every call site to spell out ``community:<id>`` would just be a
    second place for the two to drift apart — this is the same rule the
    migration backfills existing rows with.  A catalogue install has no
    ``user_skills`` row and therefore no derivable key: it must name its
    catalogue entry (``skill:web-research``, ``mcp:playwright``) explicitly,
    and saying so here beats a bare NOT NULL violation at flush time.
    """
    user_skill_id = context.get_current_parameters().get("user_skill_id")
    if not user_skill_id:
        raise ValueError(
            "skill_installs.catalog_id is required for a catalogue install "
            "(pass '<kind>:<entry id>'); only community installs derive it"
        )
    return f"community:{user_skill_id}"


class SkillInstall(Base):
    __tablename__ = "skill_installs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    #: Null for catalogue entries — those live in code, not in ``user_skills``.
    user_skill_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("user_skills.id"), nullable=True
    )
    #: ``skill`` | ``mcp``.  An MCP server is installed on its own, so install
    #: provenance has to distinguish the two kinds of catalogue entry.
    kind: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default=text("'skill'")
    )
    #: One key across both sources: ``community:<user_skills.id>`` for a
    #: published user skill, ``<kind>:<entry id>`` for a catalogue entry. It is
    #: what the admin console groups and counts installs by.
    catalog_id: Mapped[str] = mapped_column(
        String(96), nullable=False, default=_derive_catalog_id
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    install_dir: Mapped[str] = mapped_column(String(64), nullable=False)
    installed_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        # ``install_dir`` is only unique *within* a kind: a skill's directory
        # and an MCP server's name live in different namespaces and may
        # collide (a community skill called "memory", the catalogue server
        # "memory"). Keying on the directory alone made the second install
        # silently re-point the first one's provenance row.
        UniqueConstraint(
            "user_id", "kind", "install_dir", name="uq_skill_installs_user_kind_dir"
        ),
        Index("ix_skill_installs_user", "user_id", "installed_at"),
        # "who installed this entry", the admin console's per-skill view.
        Index("ix_skill_installs_catalog_installed", "catalog_id", "installed_at"),
    )
