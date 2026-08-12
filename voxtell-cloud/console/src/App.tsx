import { useEffect, useState } from "react";
import { useAuth } from "./auth/AuthProvider";
import { userManager } from "./auth/userManager";
import { api, type Me } from "./lib/api";
import { Button } from "./components/ui";
import { Jobs } from "./pages/Jobs";
import { Keys } from "./pages/Keys";

type Tab = "keys" | "jobs";

/** Completes the authorization-code exchange, then returns to the app root. */
function Callback() {
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    userManager
      .signinRedirectCallback()
      .then(() => window.history.replaceState({}, "", "/"))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);
  return (
    <Centered>
      {error ? (
        <>
          <p className="text-danger">Sign-in failed: {error}</p>
          <Button onClick={() => (window.location.href = "/")}>Try again</Button>
        </>
      ) : (
        <p className="text-muted">Signing in…</p>
      )}
    </Centered>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="relative flex min-h-screen flex-col items-center justify-center gap-4 px-6 text-center">
      {children}
    </div>
  );
}

function SignIn({ onSignin }: { onSignin: () => void }) {
  return (
    <Centered>
      <Wordmark />
      <p className="max-w-md text-sm text-muted">
        Free-text 3D segmentation for Varian Eclipse. Sign in to manage the API
        keys your ESAPI plugin uses and to follow your segmentation jobs.
      </p>
      <Button onClick={onSignin} className="mt-2">
        Sign in
      </Button>
    </Centered>
  );
}

function Wordmark() {
  return (
    <div className="flex items-center gap-2.5">
      {/* Three stacked slices with a prompt caret — text in, volume out. */}
      <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true">
        <g fill="none" stroke="var(--color-accent)" strokeWidth="1.6" strokeLinejoin="round">
          <path d="M13 3.5 22 8l-9 4.5L4 8z" />
          <path d="M4 13l9 4.5L22 13" opacity=".65" />
          <path d="M4 18l9 4.5L22 18" opacity=".35" />
        </g>
      </svg>
      <span className="text-lg font-semibold tracking-tight">
        VoxTell <span className="text-accent">Cloud</span>
      </span>
    </div>
  );
}

export function App() {
  const { isAuthenticated, isLoading, isSigningOut, token, signin, signout } = useAuth();
  const [tab, setTab] = useState<Tab>("keys");
  const [me, setMe] = useState<Me | null>(null);

  const isCallback = window.location.pathname === "/auth/callback";

  useEffect(() => {
    if (!token) return;
    api.me(token).then(setMe).catch(() => setMe(null));
  }, [token]);

  if (isCallback) return <Callback />;
  if (isLoading) return <Centered><p className="text-muted">Loading…</p></Centered>;
  if (isSigningOut) return <Centered><p className="text-muted">Signing out…</p></Centered>;
  if (!isAuthenticated || !token) return <SignIn onSignin={() => void signin()} />;

  return (
    <div className="relative mx-auto flex min-h-screen max-w-5xl flex-col px-6 py-8">
      <header className="mb-8 flex flex-wrap items-center justify-between gap-4">
        <Wordmark />
        <div className="flex items-center gap-3 text-sm">
          <span className="text-muted">{me?.email ?? me?.username ?? ""}</span>
          <Button variant="ghost" onClick={() => void signout()}>
            Sign out
          </Button>
        </div>
      </header>

      {me && (
        <p className="mb-6 text-sm text-muted">
          {me.used_this_month} of{" "}
          {me.monthly_quota === null ? "unlimited" : me.monthly_quota} jobs used
          this month · {me.outstanding}/{me.max_outstanding} in flight
        </p>
      )}

      <nav className="mb-5 flex gap-1" role="tablist">
        {(["keys", "jobs"] as const).map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={`rounded-lg px-3.5 py-2 text-sm font-medium capitalize transition-colors ${
              tab === t
                ? "bg-surface-2 text-ink"
                : "text-muted hover:bg-surface hover:text-ink"
            }`}
          >
            {t === "keys" ? "API keys" : "Jobs"}
          </button>
        ))}
      </nav>

      <main className="flex-1">
        {tab === "keys" ? <Keys token={token} /> : <Jobs token={token} />}
      </main>

      <footer className="mt-10 border-t border-border pt-5 text-xs text-muted">
        VoxTell model by MIC, DKFZ Heidelberg (arXiv:2511.11450). Segmentation
        output is for research and planning support — always review contours
        before clinical use.
      </footer>
    </div>
  );
}
