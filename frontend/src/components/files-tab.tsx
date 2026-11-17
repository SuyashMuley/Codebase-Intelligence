"use client";

import { ApiError } from "@/lib/api";
import { useFile, useFiles } from "@/lib/hooks";
import { Button, Card, EmptyState, ErrorNotice, Spinner } from "./ui";
import { CodeViewer, type LineRange } from "./code-viewer";
import { FileExplorer } from "./file-explorer";

export function FilesTab({
  repoId,
  hasSnapshot,
  refreshing,
  selected,
  range,
  onSelect,
  onSelectRange,
}: {
  repoId: string;
  hasSnapshot: boolean;
  refreshing: boolean;
  selected: string | null;
  range: LineRange | null;
  onSelect: (path: string) => void;
  onSelectRange: (range: LineRange | null) => void;
}) {
  const files = useFiles(repoId, hasSnapshot, refreshing);
  const file = useFile(repoId, selected);

  if (!hasSnapshot) {
    return (
      <EmptyState title="Nothing to browse yet">
        Index this repository from the Overview tab to explore its files.
      </EmptyState>
    );
  }
  if (files.isPending) return <Spinner label="Loading files…" />;
  if (files.isError) {
    return (
      <div className="space-y-3">
        <ErrorNotice title="Could not load the file list">{files.error.message}</ErrorNotice>
        <Button variant="secondary" onClick={() => files.refetch()}>
          Try again
        </Button>
      </div>
    );
  }
  if (files.data.items.length === 0) {
    return (
      <EmptyState title="No supported source files">
        This repository has no Python, JavaScript, TypeScript or Go files within the size limits.
      </EmptyState>
    );
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[18rem_1fr]">
      <Card className="h-[70vh] min-h-0 p-3">
        <FileExplorer entries={files.data.items} selected={selected} onSelect={onSelect} />
        {files.data.truncated ? (
          <p className="mt-2 text-xs text-zinc-500">The list is cut off; refine with the filter.</p>
        ) : null}
      </Card>
      <Card className="h-[70vh] min-h-0 overflow-hidden p-0">
        <CodeViewer
          file={selected ? file.data : undefined}
          loading={selected !== null && file.isPending}
          error={selected !== null && file.error instanceof ApiError ? file.error : null}
          range={range}
          onSelectRange={onSelectRange}
          onRetry={() => file.refetch()}
        />
      </Card>
    </div>
  );
}
