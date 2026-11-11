"""Reference examples for the Light entries (docs/LIGHT_MODE_IMPL_PLAN.md §11).

One row per approved piece of work the content team put in the library: which
entry it came from, the industry and shop type it suits, the recipe (the
review card or generation prompt as it was), and the input and output assets
by reference — the files stay where they are in the bucket, nothing is copied.
The matcher in ``light/examples.py`` reads these by entry → industry → shop
type → tags, and ``use_count`` grows each time one is shown to the model.
"""
from datetime import datetime

from sqlalchemy import Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType

ENTRY_IDS = ("video", "menu", "poster", "footage_edit")
STATUSES = ("active", "disabled")


class LightExample(Base):
    __tablename__ = "light_examples"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: video | menu | poster | footage_edit (light/taxonomy.json ``entries``).
    entry_id: Mapped[str] = mapped_column(String(32), nullable=False)
    #: food | beauty | retail | other, the first-video form's enum.
    industry: Mapped[str] = mapped_column(String(32), nullable=False)
    #: A shop type of that industry in light/taxonomy.json, or ``other``.
    shop_type: Mapped[str] = mapped_column(String(32), nullable=False, default="other", server_default=text("'other'"))
    #: The entry's subtype or sample id when known (``dish_closeup``, ``menu_red``…).
    subtype: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tags: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    #: 1–5, the team's verdict; the matcher prefers higher.
    rating: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default=text("3"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default=text("'active'"))
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    #: What makes this one good, in the team's words; shown to the model as the summary.
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    #: The review card and/or generation prompt as they were in the session.
    recipe: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    #: [{asset_id, oss_key, name, mime}] of the photos or footage the owner supplied.
    inputs: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    #: [{asset_id, oss_key, name, mime}] of the finished clip or image.
    outputs: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    #: {session_id, job_id, user_id, workspace_id, shop_name, headline} — where it came from.
    source: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    #: Times the matcher put this example in front of the model.
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))

    __table_args__ = (
        Index("ix_light_examples_match", "entry_id", "industry", "shop_type", "status"),
        Index("ix_light_examples_created", "created_at"),
    )
