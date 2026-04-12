"""add github mirror jobs and pr sync state"""

from alembic import op
import sqlalchemy as sa


revision = "20260413_0006"
down_revision = "20260412_0005"
branch_labels = None
depends_on = None


github_host_kind = sa.Enum("github_com", "ghes", name="githubhostkind", native_enum=False)
github_mirror_action = sa.Enum(
    "upsert_workspace_comment",
    "create_thread",
    "reply",
    "resolve",
    "reopen",
    name="githubmirroraction",
    native_enum=False,
)
github_mirror_job_state = sa.Enum(
    "pending",
    "processing",
    "sent",
    "failed",
    name="githubmirrorjobstate",
    native_enum=False,
)
github_mirror_state = sa.Enum(
    "pending",
    "mirrored",
    "failed",
    "skipped",
    name="githubmirrorstate",
    native_enum=False,
)


def upgrade() -> None:
    op.add_column("managed_reviews", sa.Column("github_workspace_comment_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "managed_reviews",
        sa.Column("github_workspace_comment_url", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "managed_reviews",
        sa.Column(
            "github_host_kind",
            github_host_kind,
            nullable=False,
            server_default="github_com",
        ),
    )
    op.add_column(
        "managed_reviews",
        sa.Column(
            "github_api_base_url",
            sa.String(length=512),
            nullable=False,
            server_default="https://api.github.com",
        ),
    )
    op.add_column(
        "managed_reviews",
        sa.Column(
            "github_web_base_url",
            sa.String(length=512),
            nullable=False,
            server_default="https://github.com",
        ),
    )

    op.add_column("review_threads", sa.Column("github_root_comment_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "review_threads",
        sa.Column("github_root_comment_url", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "review_threads",
        sa.Column(
            "github_mirror_state",
            github_mirror_state,
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "review_threads",
        sa.Column(
            "github_mirror_metadata_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "review_threads",
        sa.Column("github_last_mirrored_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "thread_messages",
        sa.Column("github_reply_comment_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "thread_messages",
        sa.Column("github_reply_comment_url", sa.String(length=1024), nullable=True),
    )

    op.create_table(
        "github_mirror_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("managed_review_id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=True),
        sa.Column("thread_message_id", sa.Uuid(), nullable=True),
        sa.Column("action", github_mirror_action, nullable=False),
        sa.Column(
            "state",
            github_mirror_job_state,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["managed_review_id"], ["managed_reviews.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["thread_id"], ["review_threads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["thread_message_id"], ["thread_messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_github_mirror_jobs_state_created_at",
        "github_mirror_jobs",
        ["state", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_github_mirror_jobs_review_state",
        "github_mirror_jobs",
        ["managed_review_id", "state"],
        unique=False,
    )

    op.alter_column("managed_reviews", "github_host_kind", server_default=None)
    op.alter_column("managed_reviews", "github_api_base_url", server_default=None)
    op.alter_column("managed_reviews", "github_web_base_url", server_default=None)
    op.alter_column("review_threads", "github_mirror_state", server_default=None)
    op.alter_column("review_threads", "github_mirror_metadata_json", server_default=None)
    op.alter_column("github_mirror_jobs", "state", server_default=None)
    op.alter_column("github_mirror_jobs", "attempt_count", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_github_mirror_jobs_review_state", table_name="github_mirror_jobs")
    op.drop_index("ix_github_mirror_jobs_state_created_at", table_name="github_mirror_jobs")
    op.drop_table("github_mirror_jobs")

    op.drop_column("thread_messages", "github_reply_comment_url")
    op.drop_column("thread_messages", "github_reply_comment_id")

    op.drop_column("review_threads", "github_last_mirrored_at")
    op.drop_column("review_threads", "github_mirror_metadata_json")
    op.drop_column("review_threads", "github_mirror_state")
    op.drop_column("review_threads", "github_root_comment_url")
    op.drop_column("review_threads", "github_root_comment_id")

    op.drop_column("managed_reviews", "github_web_base_url")
    op.drop_column("managed_reviews", "github_api_base_url")
    op.drop_column("managed_reviews", "github_host_kind")
    op.drop_column("managed_reviews", "github_workspace_comment_url")
    op.drop_column("managed_reviews", "github_workspace_comment_id")
