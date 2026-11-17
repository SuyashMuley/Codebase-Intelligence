"""ingestion: snapshot client_key and job error_code

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("snapshots", sa.Column("client_key", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_snapshots_client_key_created_at", "snapshots", ["client_key", "created_at"]
    )
    op.add_column("index_jobs", sa.Column("error_code", sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column("index_jobs", "error_code")
    op.drop_index("ix_snapshots_client_key_created_at", table_name="snapshots")
    op.drop_column("snapshots", "client_key")
