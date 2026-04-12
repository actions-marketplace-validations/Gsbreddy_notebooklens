"""add managed ai gateway configs"""

from alembic import op
import sqlalchemy as sa


revision = "20260412_0004"
down_revision = "20260412_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_ai_gateway_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "provider_kind",
            sa.Enum("none", "litellm", name="managedaigatewayproviderkind", native_enum=False),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column(
            "github_host_kind",
            sa.Enum("github_com", "ghes", name="githubhostkind", native_enum=False),
            nullable=False,
        ),
        sa.Column("github_api_base_url", sa.String(length=512), nullable=False),
        sa.Column("github_web_base_url", sa.String(length=512), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=False),
        sa.Column("model_name", sa.String(length=255), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("api_key_header_name", sa.String(length=255), nullable=False),
        sa.Column("static_headers_encrypted_json", sa.Text(), nullable=True),
        sa.Column("use_responses_api", sa.Boolean(), nullable=False),
        sa.Column("litellm_virtual_key_id", sa.String(length=255), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("updated_by_github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["installation_id"], ["github_installations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("installation_id"),
    )


def downgrade() -> None:
    op.drop_table("managed_ai_gateway_configs")
