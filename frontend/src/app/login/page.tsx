import { Suspense } from "react";
import { LoginCard } from "@/components/login-card";
import { Spinner } from "@/components/ui";

export default function LoginPage() {
  return (
    <div className="flex flex-1 items-center justify-center p-6">
      {/* useSearchParams needs a Suspense boundary so the rest of the page can prerender. */}
      <Suspense fallback={<Spinner />}>
        <LoginCard />
      </Suspense>
    </div>
  );
}
