"""Add media-template editions and link the historical libraries without copying assets."""

from alembic import op
import sqlalchemy as sa

from db.base import JSONType

revision = "mt8b4e6c1a2d"
down_revision = "pu1b7c8d9e0f"
branch_labels = None
depends_on = None


def _id(name="id"):
    return sa.Column(name, sa.String(64), primary_key=True)


def _fk(name, table, **kwargs):
    return sa.Column(name, sa.String(64), sa.ForeignKey(f"{table}.id"), **kwargs)


def upgrade():
    op.create_table(
        "media_templates", _id(), _fk("owner_id", "users", nullable=False),
        _fk("workspace_id", "workspaces", nullable=False), sa.Column("entry_id", sa.String(32), nullable=False),
        sa.CheckConstraint("entry_id in ('video', 'footage_edit', 'menu', 'poster')", name="ck_media_template_entry"),
    )
    op.create_index("ix_media_templates_owner_id", "media_templates", ["owner_id"])
    op.create_index("ix_media_templates_workspace_id", "media_templates", ["workspace_id"])
    op.create_table(
        "media_template_editions", _id(), _fk("template_id", "media_templates", nullable=False),
        sa.Column("version", sa.Integer, nullable=False), _fk("source_session_id", "sessions", nullable=False),
        _fk("source_asset_id", "file_assets", nullable=False),
        sa.Column("title", sa.String(120), nullable=False), sa.Column("recipe", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False), sa.Column("scene", sa.String(32)),
        sa.Column("preview_key", sa.String(512)), sa.Column("preview_mime", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("template_id", "version", name="uq_media_template_version"),
        sa.CheckConstraint("status in ('draft', 'pending', 'listed', 'rejected', 'superseded', 'withdrawn')",
                           name="ck_media_template_status"),
    )
    op.create_index("ix_media_template_editions_status", "media_template_editions", ["status"])
    op.create_table(
        "media_template_trials", _fk("session_id", "sessions", primary_key=True),
        _fk("edition_id", "media_template_editions", nullable=False), _fk("output_asset_id", "file_assets"),
        sa.Column("snapshot", JSONType), sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("session_id", "edition_id", name="uq_media_template_trial_edition"),
    )
    op.create_index("ix_media_template_trials_edition_id", "media_template_trials", ["edition_id"])
    for table in ("media_template_submissions", "media_template_reviews"):
        submission = table == "media_template_submissions"
        columns = [
            _fk("edition_id", "media_template_editions", primary_key=submission, nullable=False),
            sa.Column("trial_session_id", sa.String(64), nullable=False, unique=submission),
            sa.ForeignKeyConstraint(
                ["trial_session_id", "edition_id"], ["media_template_trials.session_id", "media_template_trials.edition_id"],
                name=f"fk_{table}_trial",
            ),
        ]
        if not submission:
            columns += [
                _id(), _fk("reviewer_id", "users", nullable=False), sa.Column("decision", sa.String(16), nullable=False),
                sa.Column("reason", sa.Text, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
                sa.CheckConstraint("decision in ('listed', 'rejected', 'withdrawn')", name="ck_media_template_decision"),
            ]
        op.create_table(table, *columns)
    op.create_index("ix_media_template_reviews_edition_id", "media_template_reviews", ["edition_id"])
    op.create_table(
        "media_template_imports", _id(),
        sa.Column("example_id", sa.String(64), sa.ForeignKey("light_examples.id", ondelete="CASCADE"), unique=True),
        sa.Column("video_edition_id", sa.String(64), sa.ForeignKey("video_template_editions.id", ondelete="CASCADE"), unique=True),
        sa.CheckConstraint("(example_id is null) <> (video_edition_id is null)", name="ck_media_template_import_source"),
    )
    op.execute("INSERT INTO media_template_imports (id, example_id) SELECT id, id FROM light_examples")
    op.execute("INSERT INTO media_template_imports (id, video_edition_id) SELECT id, id FROM video_template_editions")


def downgrade():
    for table in ("media_template_imports", "media_template_reviews", "media_template_submissions",
                  "media_template_trials", "media_template_editions", "media_templates"):
        op.drop_table(table)
