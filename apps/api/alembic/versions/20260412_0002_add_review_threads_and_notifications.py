"""add review threads and notifications"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260412_0002"
down_revision = "20260412_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "managed_reviews",
        sa.Column("pull_author_github_user_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "managed_reviews",
        sa.Column("pull_author_login", sa.String(length=255), nullable=True),
    )

    op.create_table(
        "review_threads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("managed_review_id", sa.Uuid(), nullable=False),
        sa.Column("origin_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("current_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("anchor_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.Enum("open", "resolved", "outdated", name="reviewthreadstatus", native_enum=False),
            nullable=False,
        ),
        sa.Column("carried_forward", sa.Boolean(), nullable=False),
        sa.Column("created_by_github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_github_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["current_snapshot_id"], ["review_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["managed_review_id"], ["managed_reviews.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["origin_snapshot_id"], ["review_snapshots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_review_threads_managed_review_id_status",
        "review_threads",
        ["managed_review_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_review_threads_current_snapshot_id_status",
        "review_threads",
        ["current_snapshot_id", "status"],
        unique=False,
    )

    op.create_table(
        "thread_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("author_github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("author_login", sa.String(length=255), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["review_threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "thread_created",
                "reply_added",
                "thread_resolved",
                "thread_reopened",
                name="notificationeventtype",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("recipient_github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_email", sa.String(length=320), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "delivery_state",
            sa.Enum("pending", "sent", "failed", name="notificationdeliverystate", native_enum=False),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["thread_id"], ["review_threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("notification_outbox")
    op.drop_table("thread_messages")
    op.drop_index("ix_review_threads_current_snapshot_id_status", table_name="review_threads")
    op.drop_index("ix_review_threads_managed_review_id_status", table_name="review_threads")
    op.drop_table("review_threads")
    op.drop_column("managed_reviews", "pull_author_login")
    op.drop_column("managed_reviews", "pull_author_github_user_id")
