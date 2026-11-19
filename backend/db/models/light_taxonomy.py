"""The console's edit of the example library's classification (light/taxonomy.py).

One row: the scenes of each entry and the industries with their shop types, as
the content team last saved them. No row means the file that ships is in force.
"""
from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class LightTaxonomy(Base):
    __tablename__ = "light_taxonomy"

    #: Always ``default``.
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: ``{scenes: {entry_id: [{id, label}]}, industries: [{id, label, shop_types: [{id, label, keywords}]}]}``.
    data: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
