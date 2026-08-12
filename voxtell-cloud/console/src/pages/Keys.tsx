import { useCallback, useEffect, useState } from "react";
import { api, type ApiKey, type CreatedApiKey } from "../lib/api";
import { Alert, Button, Card, Empty } from "../components/ui";

const fmt = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";

export function Keys({ token }: { token: string }) {
  const [keys, setKeys] = useState<ApiKey[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  // The plaintext exists only in this state, only until the page is left.
  const [minted, setMinted] = useState<CreatedApiKey | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    try {
      setKeys(await api.listKeys(token));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [token]);

  useEffect(() => {
    void load();
  }, [load]);

  const create = async () => {
    if (!name.trim()) return;
    setBusy(true);
    try {
      setMinted(await api.createKey(token, name.trim(), null));
      setName("");
      setCopied(false);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (key: ApiKey) => {
    if (!confirm(`Revoke "${key.name}"? Any Eclipse workstation using it stops working immediately.`))
      return;
    try {
      await api.revokeKey(token, key.id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="space-y-5">
      {minted && (
        <div className="rounded-xl border border-accent/40 bg-accent/5 p-5">
          <h3 className="text-sm font-semibold text-accent">
            Copy this key now — it is not shown again
          </h3>
          <p className="mt-1 text-sm text-muted">
            Only a hash is stored, so there is no way to recover it later. Paste it
            into the VoxTell plugin's settings in Eclipse.
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <code className="mono flex-1 overflow-x-auto rounded-lg border border-border bg-ground px-3 py-2 text-sm whitespace-nowrap">
              {minted.token}
            </code>
            <Button
              onClick={() => {
                void navigator.clipboard.writeText(minted.token).then(() => setCopied(true));
              }}
            >
              {copied ? "Copied" : "Copy"}
            </Button>
            <Button variant="ghost" onClick={() => setMinted(null)}>
              Done
            </Button>
          </div>
        </div>
      )}

      <Card title="API keys">
        {error && (
          <div className="mb-4">
            <Alert>{error}</Alert>
          </div>
        )}

        <div className="mb-5 flex flex-wrap gap-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void create()}
            placeholder="Key name, e.g. Eclipse workstation 3"
            maxLength={128}
            className="min-w-64 flex-1 rounded-lg border border-border bg-ground px-3 py-2 text-sm
                       placeholder:text-muted focus:border-accent focus:outline-none"
          />
          <Button onClick={() => void create()} disabled={busy || !name.trim()}>
            {busy ? "Creating…" : "Create key"}
          </Button>
        </div>

        {keys === null ? (
          <Empty>Loading…</Empty>
        ) : keys.length === 0 ? (
          <Empty>No keys yet. Create one to connect the Eclipse plugin.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase tracking-wide text-muted">
                <tr className="border-b border-border">
                  <th className="pb-2 pr-4 font-medium">Name</th>
                  <th className="pb-2 pr-4 font-medium">Key</th>
                  <th className="pb-2 pr-4 font-medium">Created</th>
                  <th className="pb-2 pr-4 font-medium">Last used</th>
                  <th className="pb-2 font-medium"></th>
                </tr>
              </thead>
              <tbody>
                {keys.map((k) => (
                  <tr key={k.id} className="border-b border-border/60 last:border-0">
                    <td className="py-3 pr-4">{k.name}</td>
                    <td className="mono py-3 pr-4 text-muted">{k.prefix}…</td>
                    <td className="py-3 pr-4 text-muted">{fmt(k.created_at)}</td>
                    <td className="py-3 pr-4 text-muted">{fmt(k.last_used_at)}</td>
                    <td className="py-3 text-right">
                      {k.revoked_at ? (
                        <span className="text-xs text-muted">revoked</span>
                      ) : (
                        <Button variant="danger" onClick={() => void revoke(k)}>
                          Revoke
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
