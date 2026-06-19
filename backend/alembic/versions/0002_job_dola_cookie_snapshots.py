"""add dola cookie snapshots to jobs"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0002_job_dola_cookie_snapshots"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.add_column("job", sa.Column("dola_cookie_snapshots_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    else:
        op.add_column("job", sa.Column("dola_cookie_snapshots_json", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("job", "dola_cookie_snapshots_json")
