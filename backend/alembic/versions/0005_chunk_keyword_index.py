"""chunk keyword index: split identifiers

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "to_tsvector('simple', text)"
NEW = (
    "to_tsvector('simple', "
    "regexp_replace(text, '[^A-Za-z0-9]+', ' ', 'g') || ' ' || "
    "regexp_replace(regexp_replace(text, '([a-z0-9])([A-Z])', '\\1 \\2', 'g'), "
    "'[^A-Za-z0-9]+', ' ', 'g'))"
)


def _replace_tsv(expression: str) -> None:
    # A generated column's expression cannot be altered, so rebuild it. Postgres recomputes
    # the value for every existing row when the column is added.
    op.drop_index("ix_chunks_tsv", table_name="chunks", postgresql_using="gin")
    op.drop_column("chunks", "tsv")
    op.add_column(
        "chunks",
        sa.Column(
            "tsv", postgresql.TSVECTOR(), sa.Computed(expression, persisted=True), nullable=False
        ),
    )
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")


def upgrade() -> None:
    _replace_tsv(NEW)


def downgrade() -> None:
    _replace_tsv(OLD)
