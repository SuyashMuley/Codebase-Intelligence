"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-11-17 11:30:26.911886
"""
from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    op.create_table('repositories',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('owner', sa.String(length=39), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('url', sa.String(length=200), nullable=False),
    sa.Column('default_branch', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_repositories'))
    )
    op.create_index('uq_repositories_owner_name_lower', 'repositories', [sa.literal_column('lower(owner)'), sa.literal_column('lower(name)')], unique=True)
    op.create_table('chat_usage',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('repository_id', sa.UUID(), nullable=False),
    sa.Column('client_key', sa.String(length=64), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['repository_id'], ['repositories.id'], name=op.f('fk_chat_usage_repository_id_repositories'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_chat_usage')),
    sa.UniqueConstraint('repository_id', 'client_key', 'day', name='uq_chat_usage_repository_id_client_key_day')
    )
    op.create_table('snapshots',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('repository_id', sa.UUID(), nullable=False),
    sa.Column('commit_sha', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('file_count', sa.Integer(), nullable=False),
    sa.Column('chunk_count', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('indexed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['repository_id'], ['repositories.id'], name=op.f('fk_snapshots_repository_id_repositories'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_snapshots')),
    sa.UniqueConstraint('repository_id', 'commit_sha', name='uq_snapshots_repository_id_commit_sha')
    )
    op.create_index(op.f('ix_snapshots_repository_id'), 'snapshots', ['repository_id'], unique=False)
    op.create_table('files',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('snapshot_id', sa.UUID(), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('language', sa.String(length=20), nullable=True),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('content', sa.Text(), nullable=True),
    sa.Column('skipped_reason', sa.String(length=40), nullable=True),
    sa.ForeignKeyConstraint(['snapshot_id'], ['snapshots.id'], name=op.f('fk_files_snapshot_id_snapshots'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_files')),
    sa.UniqueConstraint('snapshot_id', 'path', name='uq_files_snapshot_id_path')
    )
    op.create_table('index_jobs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('snapshot_id', sa.UUID(), nullable=False),
    sa.Column('phase', sa.String(length=20), nullable=False),
    sa.Column('cursor', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['snapshot_id'], ['snapshots.id'], name=op.f('fk_index_jobs_snapshot_id_snapshots'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_index_jobs')),
    sa.UniqueConstraint('snapshot_id', name=op.f('uq_index_jobs_snapshot_id'))
    )
    op.create_table('symbols',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('snapshot_id', sa.UUID(), nullable=False),
    sa.Column('file_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('qualified_name', sa.Text(), nullable=False),
    sa.Column('start_line', sa.Integer(), nullable=False),
    sa.Column('end_line', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['file_id'], ['files.id'], name=op.f('fk_symbols_file_id_files'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['snapshot_id'], ['snapshots.id'], name=op.f('fk_symbols_snapshot_id_snapshots'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_symbols'))
    )
    op.create_index('ix_symbols_snapshot_id_file_id', 'symbols', ['snapshot_id', 'file_id'], unique=False)
    op.create_table('chunks',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('snapshot_id', sa.UUID(), nullable=False),
    sa.Column('file_id', sa.BigInteger(), nullable=False),
    sa.Column('symbol_id', sa.BigInteger(), nullable=True),
    sa.Column('start_line', sa.Integer(), nullable=False),
    sa.Column('end_line', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('tsv', postgresql.TSVECTOR(), sa.Computed("to_tsvector('simple', text)", persisted=True), nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=True),
    sa.Column('embedding_model', sa.String(length=100), nullable=True),
    sa.ForeignKeyConstraint(['file_id'], ['files.id'], name=op.f('fk_chunks_file_id_files'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['snapshot_id'], ['snapshots.id'], name=op.f('fk_chunks_snapshot_id_snapshots'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['symbol_id'], ['symbols.id'], name=op.f('fk_chunks_symbol_id_symbols'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_chunks'))
    )
    op.create_index('ix_chunks_snapshot_id_file_id', 'chunks', ['snapshot_id', 'file_id'], unique=False)
    op.create_index('ix_chunks_tsv', 'chunks', ['tsv'], unique=False, postgresql_using='gin')
    op.create_table('graph_edges',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('snapshot_id', sa.UUID(), nullable=False),
    sa.Column('src_symbol_id', sa.BigInteger(), nullable=True),
    sa.Column('dst_symbol_id', sa.BigInteger(), nullable=True),
    sa.Column('src_file_id', sa.BigInteger(), nullable=True),
    sa.Column('dst_file_id', sa.BigInteger(), nullable=True),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('resolution', sa.String(length=20), nullable=False),
    sa.ForeignKeyConstraint(['dst_file_id'], ['files.id'], name=op.f('fk_graph_edges_dst_file_id_files'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['dst_symbol_id'], ['symbols.id'], name=op.f('fk_graph_edges_dst_symbol_id_symbols'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['snapshot_id'], ['snapshots.id'], name=op.f('fk_graph_edges_snapshot_id_snapshots'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['src_file_id'], ['files.id'], name=op.f('fk_graph_edges_src_file_id_files'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['src_symbol_id'], ['symbols.id'], name=op.f('fk_graph_edges_src_symbol_id_symbols'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_graph_edges'))
    )
    op.create_index('ix_graph_edges_snapshot_id_dst_file_id', 'graph_edges', ['snapshot_id', 'dst_file_id'], unique=False)
    op.create_index('ix_graph_edges_snapshot_id_dst_symbol_id', 'graph_edges', ['snapshot_id', 'dst_symbol_id'], unique=False)
    op.create_index('ix_graph_edges_snapshot_id_src_file_id', 'graph_edges', ['snapshot_id', 'src_file_id'], unique=False)
    op.create_index('ix_graph_edges_snapshot_id_src_symbol_id', 'graph_edges', ['snapshot_id', 'src_symbol_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_graph_edges_snapshot_id_src_symbol_id', table_name='graph_edges')
    op.drop_index('ix_graph_edges_snapshot_id_src_file_id', table_name='graph_edges')
    op.drop_index('ix_graph_edges_snapshot_id_dst_symbol_id', table_name='graph_edges')
    op.drop_index('ix_graph_edges_snapshot_id_dst_file_id', table_name='graph_edges')
    op.drop_table('graph_edges')
    op.drop_index('ix_chunks_tsv', table_name='chunks', postgresql_using='gin')
    op.drop_index('ix_chunks_snapshot_id_file_id', table_name='chunks')
    op.drop_table('chunks')
    op.drop_index('ix_symbols_snapshot_id_file_id', table_name='symbols')
    op.drop_table('symbols')
    op.drop_table('index_jobs')
    op.drop_table('files')
    op.drop_index(op.f('ix_snapshots_repository_id'), table_name='snapshots')
    op.drop_table('snapshots')
    op.drop_table('chat_usage')
    op.drop_index('uq_repositories_owner_name_lower', table_name='repositories')
    op.drop_table('repositories')
    # The vector extension is left installed on downgrade; other databases may use it.
