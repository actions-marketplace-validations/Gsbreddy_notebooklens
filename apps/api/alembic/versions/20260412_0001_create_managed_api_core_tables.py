"""create managed api core tables"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260412_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "github_installations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("github_installation_id", sa.BigInteger(), nullable=False),
        sa.Column("account_login", sa.String(length=255), nullable=False),
        sa.Column("account_type", sa.Enum("user", "organization", name="installationaccounttype", native_enum=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("github_installation_id"),
    )
    op.create_table(
        "installation_repositories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("owner", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("private", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["installation_id"], ["github_installations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("installation_id", "full_name"),
    )
    op.create_table(
        "managed_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("installation_repository_id", sa.Uuid(), nullable=False),
        sa.Column("owner", sa.String(length=255), nullable=False),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column("pull_number", sa.Integer(), nullable=False),
        sa.Column("base_branch", sa.String(length=255), nullable=False),
        sa.Column("latest_base_sha", sa.String(length=255), nullable=False),
        sa.Column("latest_head_sha", sa.String(length=255), nullable=False),
        sa.Column("status", sa.Enum("pending", "ready", "failed", "closed", name="managedreviewstatus", native_enum=False), nullable=False),
        sa.Column("latest_check_run_id", sa.BigInteger(), nullable=True),
        sa.Column("latest_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["installation_repository_id"], ["installation_repositories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("installation_repository_id", "pull_number"),
    )
    op.create_table(
        "snapshot_build_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("managed_review_id", sa.Uuid(), nullable=False),
        sa.Column("base_sha", sa.String(length=255), nullable=False),
        sa.Column("head_sha", sa.String(length=255), nullable=False),
        sa.Column("status", sa.Enum("queued", "running", "retryable_failed", "failed", "succeeded", name="snapshotbuildjobstatus", native_enum=False), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["managed_review_id"], ["managed_reviews.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_snapshot_build_jobs_status_scheduled_at",
        "snapshot_build_jobs",
        ["status", "scheduled_at"],
        unique=False,
    )
    op.create_table(
        "review_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("managed_review_id", sa.Uuid(), nullable=False),
        sa.Column("base_sha", sa.String(length=255), nullable=False),
        sa.Column("head_sha", sa.String(length=255), nullable=False),
        sa.Column("snapshot_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum("pending", "ready", "failed", name="reviewsnapshotstatus", native_enum=False), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=True),
        sa.Column("flagged_findings_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reviewer_guidance_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("snapshot_payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("notebook_count", sa.Integer(), nullable=False),
        sa.Column("changed_cell_count", sa.Integer(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["managed_review_id"], ["managed_reviews.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("managed_review_id", "snapshot_index"),
    )
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("github_login", sa.String(length=255), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_sessions_github_user_id", "user_sessions", ["github_user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_user_sessions_github_user_id", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_table("review_snapshots")
    op.drop_index("ix_snapshot_build_jobs_status_scheduled_at", table_name="snapshot_build_jobs")
    op.drop_table("snapshot_build_jobs")
    op.drop_table("managed_reviews")
    op.drop_table("installation_repositories")
    op.drop_table("github_installations")
