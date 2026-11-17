import { AuthGate } from "@/components/auth-gate";
import { ConnectForm } from "@/components/connect-form";
import { RepoList } from "@/components/repo-list";

export default function ReposPage() {
  return (
    <AuthGate>
      <div className="space-y-8">
        <section>
          <h1 className="mb-4 text-xl font-semibold tracking-tight">Your repositories</h1>
          <ConnectForm />
        </section>
        <RepoList />
      </div>
    </AuthGate>
  );
}
