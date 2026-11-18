"""private repositories: owner-scoped rows and the time access was last confirmed

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NIL = "'00000000-0000-0000-0000-000000000000'::uuid"


def upgrade() -> None:
    op.add_column(
        "repositories",
        sa.Column("is_private", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "repositories",
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.add_column(
        "repositories", sa.Column("access_checked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_repositories_owner_user_id", "repositories", ["owner_user_id"])
    op.create_check_constraint(
        "ck_repositories_private_has_owner",
        "repositories",
        "(is_private AND owner_user_id IS NOT NULL) OR (NOT is_private AND owner_user_id IS NULL)",
    )
    # Public repositories stay unique by name; private ones are unique per owner.
    op.drop_index("uq_repositories_owner_name_lower", table_name="repositories")
    op.execute(
        "CREATE UNIQUE INDEX uq_repositories_owner_name_lower ON repositories "
        f"(lower(owner), lower(name), coalesce(owner_user_id, {_NIL}))"
    )


def downgrade() -> None:
    # Private rows cannot exist in the old schema (it has no owner column): remove them first.
    op.execute("DELETE FROM repositories WHERE is_private")
    op.drop_index("uq_repositories_owner_name_lower", table_name="repositories")
    op.create_index(
        "uq_repositories_owner_name_lower",
        "repositories",
        [sa.literal_column("lower(owner)"), sa.literal_column("lower(name)")],
        unique=True,
    )
    op.drop_constraint("ck_repositories_private_has_owner", "repositories", type_="check")
    op.drop_index("ix_repositories_owner_user_id", table_name="repositories")
    op.drop_column("repositories", "access_checked_at")
    op.drop_column("repositories", "owner_user_id")
    op.drop_column("repositories", "is_private")
