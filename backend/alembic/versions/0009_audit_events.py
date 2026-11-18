"""audit log of private-repository access and provider changes

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("repository_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("repository", sa.String(140), nullable=True),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("request_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_audit_events_user_id_at", "audit_events", ["user_id", "at"])
    op.create_index("ix_audit_events_repository_id_at", "audit_events", ["repository_id", "at"])


def downgrade() -> None:
    op.drop_table("audit_events")
