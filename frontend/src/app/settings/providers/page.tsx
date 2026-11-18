import { AuthGate } from "@/components/auth-gate";
import { ProviderSettings } from "@/components/provider-settings";

export default function ProviderSettingsPage() {
  return (
    <AuthGate>
      <div className="space-y-6">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Provider settings</h1>
          <p className="mt-1 text-sm text-zinc-400">
            Your own embedding and chat providers, used only for your private repositories.
          </p>
        </div>
        <ProviderSettings />
      </div>
    </AuthGate>
  );
}
