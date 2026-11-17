import { Suspense } from "react";
import { RepoPageClient } from "@/components/repo-page-client";
import { Spinner } from "@/components/ui";

export default function RepoPage() {
  // useParams() needs a Suspense boundary so the shell can prerender while the id streams in.
  return (
    <Suspense
      fallback={
        <div className="flex flex-1 items-center justify-center p-8">
          <Spinner />
        </div>
      }
    >
      <RepoPageClient />
    </Suspense>
  );
}
