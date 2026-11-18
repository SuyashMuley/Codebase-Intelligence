import { AuthGate } from "@/components/auth-gate";
import { ConnectForm } from "@/components/connect-form";
import { GithubRepoPicker } from "@/components/github-repo-picker";
import { PrivateRepoPicker } from "@/components/private-repo-picker";
import { RepoList } from "@/components/repo-list";

export default function ReposPage() {
  return (
    <AuthGate>
      <div className="space-y-10">
        <section>
          <h1 className="mb-4 text-xl font-semibold tracking-tight">Your repositories</h1>
          <ConnectForm />
        </section>
        <section aria-labelledby="connected-heading" className="space-y-3">
          <h2 id="connected-heading" className="text-sm font-medium text-zinc-300">
            Connected
          </h2>
          <RepoList />
        </section>
        <section aria-labelledby="github-heading" className="space-y-3">
          <h2 id="github-heading" className="text-sm font-medium text-zinc-300">
            From your GitHub account
          </h2>
          <GithubRepoPicker />
        </section>
        <section aria-labelledby="private-heading" className="space-y-3">
          <h2 id="private-heading" className="text-sm font-medium text-zinc-300">
            Private repositories
          </h2>
          <PrivateRepoPicker />
        </section>
      </div>
    </AuthGate>
  );
}
