"""add encrypted Dola cookie profiles and usage ledgers"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0003_dola_cookie_profiles"
down_revision = "0002_job_dola_cookie_snapshots"
branch_labels = None
depends_on = None


def _json_type(bind):
    return postgresql.JSONB() if bind.dialect.name == "postgresql" else sa.JSON()


def upgrade() -> None:
    bind = op.get_bind()
    json_type = _json_type(bind)
    op.create_table(
        "dolacookieprofile",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("cookies_encrypted", sa.Text(), nullable=False),
        sa.Column("cookie_names_json", json_type, nullable=False),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
        sa.Column("validation_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_dolacookieprofile_name", "dolacookieprofile", ["name"])
    op.create_table(
        "dolacookieusage",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("usage_day", sa.String(length=10), nullable=False),
        sa.Column("completed_count", sa.Integer(), nullable=False),
        sa.Column("reserved_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("profile_id", "usage_day", name="uq_dola_cookie_usage_profile_day"),
    )
    op.create_index("ix_dolacookieusage_profile_id", "dolacookieusage", ["profile_id"])
    op.create_index("ix_dolacookieusage_usage_day", "dolacookieusage", ["usage_day"])
    op.create_table(
        "dolacookiejobsnapshot",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("job.id"), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("profile_name", sa.String(length=160), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("cookies_encrypted", sa.Text(), nullable=False),
        sa.Column("cookie_names_json", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_dolacookiejobsnapshot_job_id", "dolacookiejobsnapshot", ["job_id"])
    op.create_index("ix_dolacookiejobsnapshot_profile_id", "dolacookiejobsnapshot", ["profile_id"])


def downgrade() -> None:
    op.drop_table("dolacookiejobsnapshot")
    op.drop_table("dolacookieusage")
    op.drop_index("ix_dolacookieprofile_name", table_name="dolacookieprofile")
    op.drop_table("dolacookieprofile")
