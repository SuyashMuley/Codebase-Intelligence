"""graph edges: what the code said, why it is unresolved, where, and how often

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("graph_edges", sa.Column("target_name", sa.Text(), nullable=True))
    op.add_column("graph_edges", sa.Column("reason", sa.String(30), nullable=True))
    op.add_column("graph_edges", sa.Column("line", sa.Integer(), nullable=True))
    op.add_column(
        "graph_edges", sa.Column("count", sa.Integer(), nullable=False, server_default="1")
    )
    op.create_index(
        "ix_graph_edges_snapshot_id_kind_resolution",
        "graph_edges",
        ["snapshot_id", "kind", "resolution"],
    )


def downgrade() -> None:
    op.drop_index("ix_graph_edges_snapshot_id_kind_resolution", table_name="graph_edges")
    op.drop_column("graph_edges", "count")
    op.drop_column("graph_edges", "line")
    op.drop_column("graph_edges", "reason")
    op.drop_column("graph_edges", "target_name")
