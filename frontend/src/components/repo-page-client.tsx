"use client";

import { useParams } from "next/navigation";
import { AuthGate } from "./auth-gate";
import { RepoDetail } from "./repo-detail";

export function RepoPageClient() {
  const { id } = useParams<{ id: string }>();
  return (
    <AuthGate>
      <RepoDetail id={id} />
    </AuthGate>
  );
}
