"""initial schema"""

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("done", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_table(
        "jobitem",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), primary_key=True),
        sa.Column("job_id", sqlmodel.sql.sqltypes.GUID(), sa.ForeignKey("job.id"), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=240), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("artifact_id", sqlmodel.sql.sqltypes.GUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "artifact",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), primary_key=True),
        sa.Column("job_id", sqlmodel.sql.sqltypes.GUID(), sa.ForeignKey("job.id"), nullable=False),
        sa.Column("item_id", sqlmodel.sql.sqltypes.GUID(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("filename", sa.String(length=260), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "logevent",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), primary_key=True),
        sa.Column("job_id", sqlmodel.sql.sqltypes.GUID(), nullable=True),
        sa.Column("level", sa.String(length=20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "setting",
        sa.Column("key", sa.String(length=120), primary_key=True),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("setting")
    op.drop_table("logevent")
    op.drop_table("artifact")
    op.drop_table("jobitem")
    op.drop_table("job")
