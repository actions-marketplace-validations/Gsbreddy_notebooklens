"""add force rebuild flag to snapshot jobs"""

from alembic import op
import sqlalchemy as sa


revision = "20260412_0005"
down_revision = "20260412_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "snapshot_build_jobs",
        sa.Column("force_rebuild", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("snapshot_build_jobs", "force_rebuild", server_default=None)


def downgrade() -> None:
    op.drop_column("snapshot_build_jobs", "force_rebuild")
