import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = 384

TSV_EXPRESSION = (
    "to_tsvector('simple', "
    "regexp_replace(text, '[^A-Za-z0-9]+', ' ', 'g') || ' ' || "
    "regexp_replace(regexp_replace(text, '([a-z0-9])([A-Z])', '\\1 \\2', 'g'), "
    "'[^A-Za-z0-9]+', ' ', 'g'))"
)

# Stable constraint names keep Alembic migrations deterministic.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    login: Mapped[str] = mapped_column(String(39))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserSession(Base):
    """Server-side session. Only a hash of the cookie value is stored."""

    __tablename__ = "sessions"

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class GithubCredential(Base):
    """A user's GitHub tokens, encrypted at rest. Never returned by the API."""

    __tablename__ = "github_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    access_token_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserRepository(Base):
    """Which repositories a user connected: the basis of every access check."""

    __tablename__ = "user_repositories"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(39))
    name: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(200))
    default_branch: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # GitHub names are case-insensitive, so uniqueness is enforced on the lowercased pair.
    __table_args__ = (
        Index("uq_repositories_owner_name_lower", func.lower(owner), func.lower(name), unique=True),
    )


class Snapshot(Base):
    __tablename__ = "snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    commit_sha: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    # Who asked for this snapshot; drives the daily ingestion quota.
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # One snapshot per (repo, commit) is what blocks duplicate ingestion.
    __table_args__ = (
        UniqueConstraint(
            "repository_id", "commit_sha", name="uq_snapshots_repository_id_commit_sha"
        ),
        Index("ix_snapshots_requested_by_created_at", "requested_by", "created_at"),
    )


class IndexJob(Base):
    __tablename__ = "index_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("snapshots.id", ondelete="CASCADE"), unique=True
    )
    phase: Mapped[str] = mapped_column(String(20), default="fetch")
    cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class File(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(20))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    # NULL when the file was skipped (see skipped_reason) or dropped by secret handling.
    content: Mapped[str | None] = mapped_column(Text)
    skipped_reason: Mapped[str | None] = mapped_column(String(40))
    # How many secret-looking values were replaced before this text was stored.
    redactions: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # ok | syntax_errors | truncated, set by the parse phase.
    parse_status: Mapped[str | None] = mapped_column(String(16))

    __table_args__ = (UniqueConstraint("snapshot_id", "path", name="uq_files_snapshot_id_path"),)


class Symbol(Base):
    __tablename__ = "symbols"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"))
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(Text)
    qualified_name: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"))
    signature: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_symbols_snapshot_id_file_id", "snapshot_id", "file_id"),)


class CodeRef(Base):
    """An import or call found in a file. Calls are recorded by name and receiver only; the
    graph milestone resolves what it can and leaves the rest unresolved."""

    __tablename__ = "code_refs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"))
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    symbol_id: Mapped[int | None] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(10))  # import | call
    name: Mapped[str] = mapped_column(Text)
    line: Mapped[int] = mapped_column(Integer)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        Index("ix_code_refs_snapshot_id_file_id", "snapshot_id", "file_id"),
        Index("ix_code_refs_snapshot_id_kind_name", "snapshot_id", "kind", "name"),
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"))
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    symbol_id: Mapped[int | None] = mapped_column(ForeignKey("symbols.id", ondelete="SET NULL"))
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    # Keyword index over two clean word lists: every alphanumeric word as written (so a whole
    # identifier like sessionCookie matches), and the same text with camelCase split (so
    # "session cookie" matches it too). Punctuation and underscores never glue words together.
    # 'simple' config: no stemming or stop words, which suits identifiers in code.
    tsv: Mapped[str] = mapped_column(TSVECTOR, Computed(TSV_EXPRESSION, persisted=True))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(100))

    __table_args__ = (
        Index("ix_chunks_snapshot_id_file_id", "snapshot_id", "file_id"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        # No vector index on purpose. Every search is scoped to one snapshot (at most a few
        # thousand rows), so an exact scan is fast and always correct, while an approximate
        # (HNSW) index combined with a filter can silently return too few results.
    )


class GraphEdge(Base):
    __tablename__ = "graph_edges"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"))
    src_symbol_id: Mapped[int | None] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"))
    dst_symbol_id: Mapped[int | None] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"))
    src_file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    dst_file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))  # imports | calls | contains
    resolution: Mapped[str] = mapped_column(String(20))  # static | unresolved

    __table_args__ = (
        Index("ix_graph_edges_snapshot_id_src_symbol_id", "snapshot_id", "src_symbol_id"),
        Index("ix_graph_edges_snapshot_id_dst_symbol_id", "snapshot_id", "dst_symbol_id"),
        Index("ix_graph_edges_snapshot_id_src_file_id", "snapshot_id", "src_file_id"),
        Index("ix_graph_edges_snapshot_id_dst_file_id", "snapshot_id", "dst_file_id"),
    )


class ChatUsage(Base):
    __tablename__ = "chat_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    client_key: Mapped[str] = mapped_column(String(64))
    day: Mapped[date] = mapped_column(Date)
    count: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint(
            "repository_id", "client_key", "day", name="uq_chat_usage_repository_id_client_key_day"
        ),
    )
