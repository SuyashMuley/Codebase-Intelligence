"""user-supplied providers for private repositories (keys stored encrypted, or not at all)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_providers",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("slot", sa.String(10), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("key_enc", sa.Text(), nullable=True),
        sa.Column("key_last4", sa.String(4), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("account_id", sa.String(32), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("slot IN ('embedding', 'chat')", name="ck_user_providers_slot"),
        sa.CheckConstraint("mode IN ('stored', 'browser')", name="ck_user_providers_mode"),
        sa.CheckConstraint(
            "(mode = 'stored' AND key_enc IS NOT NULL) OR (mode = 'browser' AND key_enc IS NULL)",
            name="ck_user_providers_key_matches_mode",
        ),
    )


def downgrade() -> None:
    op.drop_table("user_providers")
