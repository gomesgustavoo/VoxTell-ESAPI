// The router and the auth gate.
//
// Was: `window.location.pathname === "/dashboard/auth/callback"` read during render,
// two tabs held in useState, no URLs, and back/forward doing nothing. Now
// react-router with basename="/dashboard", so every view is linkable and the browser's
// own navigation works.

import { Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";

import { useAuth } from "./auth/AuthProvider";
import { userManager } from "./auth/userManager";
import { AppShell, Centered, Wordmark } from "./components/layout";
import { Alert, Button } from "./components/ui";
import Billing from "./pages/Billing";
import Checkout from "./pages/Checkout";
import Jobs from "./pages/Jobs";
import Keys from "./pages/Keys";
import Overview from "./pages/Overview";

/** Completes the authorization-code exchange, then returns to the app root. */
function Callback() {
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    userManager
      .signinRedirectCallback()
      // An explicit route transition, not a history.replaceState. The old code
      // rewrote the URL and relied on the coincidence that AuthProvider's UserLoaded
      // event happened to re-render at the same moment; if that event ordering ever
      // changed, the app would sit on a rewritten URL showing the callback screen.
      .then(() => navigate("/", { replace: true }))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [navigate]);

  return (
    <Centered>
      {error ? (
        <div className="flex flex-col items-center gap-4">
          <Alert>Sign-in failed: {error}</Alert>
          <Button variant="primary" onClick={() => window.location.assign("/dashboard/")}>
            Try again
          </Button>
        </div>
      ) : (
        <p className="text-sm text-muted">Signing in…</p>
      )}
    </Centered>
  );
}

function SignIn({ onSignin, error }: { onSignin: () => void; error: Error | null }) {
  return (
    <Centered>
      <div className="flex flex-col items-center gap-4">
        <Wordmark />
        <p className="max-w-sm text-sm text-muted">
          Free-text organ segmentation for Varian Eclipse. Sign in to follow your jobs,
          manage the workstation keys your ESAPI plugin uses, and see your usage.
        </p>
        {/* AuthProvider has always set this and nothing ever rendered it, so a silent
            renew failure looked like an app that simply would not sign in. */}
        {error && <Alert>{error.message}</Alert>}
        <Button variant="primary" size="lg" onClick={onSignin}>
          Sign in
        </Button>
        <p className="text-xs text-faint">
          Uses your RT Medical account — the same sign-in as DicomSegVR.
        </p>
      </div>
    </Centered>
  );
}

function NotFound() {
  return (
    <Centered>
      <div className="flex flex-col items-center gap-4">
        <p className="text-grad text-2xl font-bold">404</p>
        <p className="text-sm text-muted">That page does not exist in the dashboard.</p>
        <Button onClick={() => window.location.assign("/dashboard/")}>Go to overview</Button>
      </div>
    </Centered>
  );
}

export function App() {
  const { isAuthenticated, isLoading, isSigningOut, token, error, signin, signout } = useAuth();

  return (
    <Routes>
      {/* The callback is outside the auth gate on purpose: at this point the user is
          by definition not yet authenticated, and gating it would bounce the code
          exchange back into a fresh sign-in. */}
      <Route path="/auth/callback" element={<Callback />} />
      <Route
        path="*"
        element={
          isLoading ? (
            <Centered>
              <p className="text-sm text-muted">Loading…</p>
            </Centered>
          ) : isSigningOut ? (
            <Centered>
              <p className="text-sm text-muted">Signing out…</p>
            </Centered>
          ) : !isAuthenticated || !token ? (
            <SignIn onSignin={() => void signin()} error={error} />
          ) : (
            <SignedIn onSignOut={() => void signout()} />
          )
        }
      />
    </Routes>
  );
}

function SignedIn({ onSignOut }: { onSignOut: () => void }) {
  const { user } = useAuth();
  const email =
    (user?.profile?.email as string | undefined) ??
    (user?.profile?.preferred_username as string | undefined) ??
    null;

  return (
    <AppShell email={email} onSignOut={onSignOut}>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/jobs" element={<Jobs />} />
        <Route path="/keys" element={<Keys />} />
        <Route path="/billing" element={<Billing />} />
        <Route path="/checkout" element={<Checkout />} />
        {/* A signed-in user hitting the callback path directly has nothing to
            exchange; send them home rather than showing "Signing in…" forever. */}
        <Route path="/auth/callback" element={<Navigate to="/" replace />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </AppShell>
  );
}
