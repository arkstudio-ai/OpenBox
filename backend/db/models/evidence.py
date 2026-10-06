"""Commit-time versions of the facts that assistant validation verdicts read.

See ``assistant.evidence_cache``. One row per (owner, covered table) records
the last committed change to that owner's rows; its value is unique for every
bump (a sequence, or random on SQLite), so a rolled-back or reused number can
never make an older verdict look current.
"""
from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class AssistantEvidenceEpoch(Base):
    __tablename__ = "assistant_evidence_epochs"

    scope_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)


from db.evidence_schema import register as _register_coverage  # noqa: E402

_register_coverage(Base.metadata)
