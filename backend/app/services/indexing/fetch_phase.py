"""Fetch phase: download the pinned commit's tarball and store its source files.

The whole phase runs in the runner's single transaction. If anything fails, nothing is
stored and the next tick starts over; because the tarball is size-capped this stays bounded.
"""

from sqlalchemy import delete, insert

from app.db.models import File
from app.services.github.extract import ExtractedFile, FetchLimits, FetchStats, extract_files
from app.services.indexing.runner import PhaseContext, PhaseResult, register

# Rows are flushed in batches so memory stays bounded; module-level so tests can shrink them.
BATCH_ROWS = 200
BATCH_BYTES = 2_000_000


def _row(snapshot_id: object, f: ExtractedFile) -> dict[str, object]:
    return {
        "snapshot_id": snapshot_id,
        "path": f.path,
        "language": f.language,
        "size_bytes": f.size_bytes,
        "sha256": f.sha256,
        "content": f.content,
        "skipped_reason": f.skipped_reason,
        "redactions": f.redactions,
    }


@register("fetch")
def run_fetch(ctx: PhaseContext) -> PhaseResult:
    s = ctx.settings
    limits = FetchLimits(
        max_file_bytes=s.max_file_bytes,
        max_files=s.max_files,
        max_total_bytes=s.max_total_source_bytes,
        max_uncompressed_bytes=s.max_uncompressed_bytes,
        max_members=s.max_tar_members,
    )
    stats = FetchStats()
    snapshot_id = ctx.snapshot.id

    # A previous attempt may have been rolled back, but be safe against leftovers.
    ctx.db.execute(delete(File).where(File.snapshot_id == snapshot_id))

    batch: list[dict[str, object]] = []
    batch_bytes = 0
    with ctx.gh.open_tarball(
        ctx.repository.owner, ctx.repository.name, ctx.snapshot.commit_sha, s.max_tarball_bytes
    ) as source:
        for f in extract_files(source, limits, ctx.deadline, stats):
            if not f.stored:
                continue
            batch.append(_row(snapshot_id, f))
            batch_bytes += len(f.content or "")
            if len(batch) >= BATCH_ROWS or batch_bytes >= BATCH_BYTES:
                ctx.db.execute(insert(File), batch)
                batch, batch_bytes = [], 0
    if batch:
        ctx.db.execute(insert(File), batch)

    ctx.snapshot.file_count = stats.files_kept
    return PhaseResult(next_phase="parse", cursor={"fetch": stats.as_dict()})
