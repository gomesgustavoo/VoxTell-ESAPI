// Workstation keys.
//
// The one-time-reveal copy and the revoke warning are kept close to verbatim — they
// are the two places on this page where getting the wording wrong costs a clinic real
// time.
//
// New here: THE EXPIRY PICKER. The server has always accepted `expires_in_days`
// (api/schemas.py::ApiKeyCreateRequest, 1..3650) and the old UI passed a hard-coded
// `null`, so every key ever minted from this console is non-expiring. A key pasted
// into a workstation that is later decommissioned then lives forever.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type ApiKey, type CreatedApiKey } from "../lib/api";
import { relative, stamp } from "../lib/format";
import { useAuth } from "../auth/AuthProvider";
import {
  Alert,
  Button,
  Card,
  Cell,
  CopyButton,
  Empty,
  Field,
  Input,
  Modal,
  Row,
  SectionHeader,
  Select,
  Skeleton,
  Table,
  ToastStack,
  useToasts,
} from "../components/ui";

const EXPIRY_CHOICES: { label: string; days: number | null }[] = [
  { label: "90 days", days: 90 },
  { label: "180 days", days: 180 },
  { label: "1 year", days: 365 },
  { label: "2 years", days: 730 },
  { label: "Never expires", days: null },
];

function keyStatus(k: ApiKey): { text: string; tone: string } {
  if (k.revoked_at) return { text: "revoked", tone: "text-faint" };
  if (k.expires_at && new Date(k.expires_at) <= new Date())
    return { text: "expired", tone: "text-faint" };
  if (k.expires_at) return { text: `expires ${stamp(k.expires_at)}`, tone: "text-muted" };
  return { text: "no expiry", tone: "text-ink-dim" };
}

export default function Keys() {
  const { token } = useAuth();
  const qc = useQueryClient();
  const { toasts, push, dismiss } = useToasts();

  const [name, setName] = useState("");
  // 365 days rather than "never" as the default: the safe option should be the one
  // that requires no thought.
  const [expiryIndex, setExpiryIndex] = useState(2);
  const [minted, setMinted] = useState<CreatedApiKey | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState<ApiKey | null>(null);

  const keys = useQuery({
    queryKey: ["keys"],
    queryFn: () => api.listKeys(token!),
    enabled: !!token,
    staleTime: 30_000,
  });

  const create = useMutation({
    mutationFn: () => api.createKey(token!, name.trim(), EXPIRY_CHOICES[expiryIndex].days),
    onSuccess: (k) => {
      setMinted(k);
      setName("");
      qc.invalidateQueries({ queryKey: ["keys"] });
    },
    onError: (e: Error) => push(e.message, "danger"),
  });

  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeKey(token!, id),
    onSuccess: () => {
      push("Key revoked.");
      qc.invalidateQueries({ queryKey: ["keys"] });
    },
    onError: (e: Error) => push(e.message, "danger"),
  });

  const live = (keys.data ?? []).filter((k) => !k.revoked_at);

  return (
    <div>
      <SectionHeader
        label="Workstation keys"
        metric={keys.data ? `${live.length} active · ${keys.data.length} total` : undefined}
      >
        One key per workstation, so a decommissioned machine can be revoked without
        touching the rest of the clinic. Paste a key into the VoxTell plugin's settings
        in Eclipse.
      </SectionHeader>

      <div className="flex flex-col gap-4">
        <Card title="Create a key">
          <div className="grid gap-3 sm:grid-cols-[1fr_12rem_auto] sm:items-end">
            <Field label="Label" hint="Name the workstation — that is what makes a key revocable with confidence.">
              <Input
                value={name}
                maxLength={128}
                placeholder="Planning room 2 — TPS-04"
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && name.trim()) create.mutate();
                }}
              />
            </Field>
            <Field label="Expires">
              <Select
                value={expiryIndex}
                onChange={(e) => setExpiryIndex(Number(e.target.value))}
              >
                {EXPIRY_CHOICES.map((c, i) => (
                  <option key={c.label} value={i}>
                    {c.label}
                  </option>
                ))}
              </Select>
            </Field>
            <Button
              variant="primary"
              disabled={!name.trim() || create.isPending}
              onClick={() => create.mutate()}
            >
              {create.isPending ? "Creating…" : "Create key"}
            </Button>
          </div>
        </Card>

        <Card title="Keys" padded={false}>
          {keys.isLoading ? (
            <div className="flex flex-col gap-2 p-5">
              {[0, 1].map((i) => (
                <Skeleton key={i} className="h-8 w-full" />
              ))}
            </div>
          ) : keys.isError ? (
            <div className="p-5">
              <Alert>Could not load keys: {(keys.error as Error).message}</Alert>
            </div>
          ) : !keys.data?.length ? (
            <div className="p-5">
              <Empty>No keys yet. Create one to connect the Eclipse plugin.</Empty>
            </div>
          ) : (
            <Table head={["Label", "Key", "Created", "Last used", "Status", ""]}>
              {keys.data.map((k) => {
                const status = keyStatus(k);
                return (
                  <Row key={k.id}>
                    <Cell className={k.revoked_at ? "text-faint" : "text-ink"}>{k.name}</Cell>
                    <Cell mono className="text-muted">
                      {k.prefix}…
                    </Cell>
                    <Cell mono className="text-muted">
                      {stamp(k.created_at)}
                    </Cell>
                    <Cell mono className="text-muted">
                      {k.last_used_at ? relative(k.last_used_at) : "never"}
                    </Cell>
                    <Cell mono className={status.tone}>
                      {status.text}
                    </Cell>
                    <Cell className="text-right">
                      {!k.revoked_at && (
                        <Button
                          size="sm"
                          variant="danger"
                          disabled={revoke.isPending}
                          onClick={() => setConfirmRevoke(k)}
                        >
                          Revoke
                        </Button>
                      )}
                    </Cell>
                  </Row>
                );
              })}
            </Table>
          )}
        </Card>
      </div>

      {/* The one-time reveal. A modal rather than an inline panel, because it must be
          impossible to scroll past the only copy of a credential. */}
      <Modal
        open={minted !== null}
        title="Copy this key now — it is not shown again"
        onClose={() => setMinted(null)}
        footer={
          <Button size="sm" variant="primary" onClick={() => setMinted(null)}>
            Done
          </Button>
        }
      >
        <p className="text-sm text-muted">
          Only a hash is stored, so there is no way to recover it later. Paste it into
          the VoxTell plugin's settings in Eclipse.
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <code className="min-w-0 flex-1 overflow-x-auto rounded-chip border border-border bg-ground px-3 py-2 text-sm whitespace-nowrap select-all">
            {minted?.token}
          </code>
          {minted && <CopyButton value={minted.token} />}
        </div>
        {minted?.expires_at && (
          <p className="mt-3 font-mono text-xs text-muted">
            Expires {stamp(minted.expires_at)}.
          </p>
        )}
      </Modal>

      <Modal
        open={confirmRevoke !== null}
        title="Revoke this key?"
        onClose={() => setConfirmRevoke(null)}
        footer={
          <>
            <Button size="sm" variant="subtle" onClick={() => setConfirmRevoke(null)}>
              Keep it
            </Button>
            <Button
              size="sm"
              variant="danger"
              onClick={() => {
                if (confirmRevoke) revoke.mutate(confirmRevoke.id);
                setConfirmRevoke(null);
              }}
            >
              Revoke
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-dim">
          Any Eclipse workstation using <b className="text-ink">{confirmRevoke?.name}</b> stops
          working immediately.
        </p>
        <p className="mt-2 text-sm text-muted">
          Jobs already running are unaffected. This cannot be undone — mint a new key to
          restore access.
        </p>
      </Modal>

      <ToastStack toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
