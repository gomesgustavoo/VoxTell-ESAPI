// The router and the auth gate.
//
// Was: `window.location.pathname === "/dashboard/auth/callback"` read during render,
// two tabs held in useState, no URLs, and back/forward doing nothing. Now
// react-router with basename="/dashboard", so every view is linkable and the browser's
// own navigation works.

import { Link, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";

import { useAuth } from "./auth/AuthProvider";
import { userManager } from "./auth/userManager";
import { AppShell, Centered, Wordmark } from "./components/layout";
import { Alert, Button, Spinner, buttonClass } from "./components/ui";
import Billing from "./pages/Billing";
import Checkout from "./pages/Checkout";
import JobDetail from "./pages/JobDetail";
import Jobs from "./pages/Jobs";
import Keys from "./pages/Keys";
import Overview from "./pages/Overview";

/**
 * The document title, per route.
 *
 * Every view shared one title, so a user with the dashboard, the landing page and
 * Eclipse's browser window open had three identically-named tabs. Titles are set
 * here rather than in each page so the list is visible in one place and cannot
 * silently go missing when a route is added.
 */
const TITLES: Record<string, string> = {
  "/": "Overview",
  "/jobs": "Jobs",
  "/keys": "Workstation keys",
  "/billing": "Billing",
  "/checkout": "Checkout",
};

function useDocumentTitle() {
  const { pathname } = useLocation();
  useEffect(() => {
    const named =
      TITLES[pathname] ?? (pathname.startsWith("/jobs/") ? "Job" : "Not found");
    document.title = `${named} · VoxTell`;
  }, [pathname]);
}

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
          <Wordmark />
          <Alert>Sign-in failed: {error}</Alert>
          {/* A fresh authorization request, not a reload of this URL: the code in
              the query string has already been redeemed and replaying it fails. */}
          <Button variant="primary" onClick={() => void userManager.signinRedirect()}>
            Try again
          </Button>
        </div>
      ) : (
        <div className="flex flex-col items-center gap-4">
          <Wordmark />
          <Spinner label="Signing in…" />
        </div>
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
    // `bare` because this renders INSIDE AppShell, which already mounts the mesh
    // background. Without it two fixed mesh layers stacked and the 404 route was
    // visibly lighter than every other page.
    <Centered bare>
      <div className="flex flex-col items-center gap-4">
        {/* The one place the app uses its own display scale. --text-3xl and
            --tracking-display were defined and referenced nowhere, so the largest
            type in the whole console was a 2xl stat tile. */}
        <p className="text-grad font-mono text-3xl font-semibold tracking-display">404</p>
        <p className="text-sm text-muted">That page does not exist in the dashboard.</p>
        {/* A router Link. window.location.assign reloaded the entire SPA — new
            bundle parse, new token exchange, every query refetched — to move between
            two routes it already had mounted. */}
        <Link to="/" className={buttonClass("ghost", "md")}>
          Go to overview
        </Link>
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
              <div className="flex flex-col items-center gap-4">
                <Wordmark />
                <Spinner label="Loading your account…" />
              </div>
            </Centered>
          ) : isSigningOut ? (
            <Centered>
              <div className="flex flex-col items-center gap-4">
                <Wordmark />
                <Spinner label="Signing out…" />
              </div>
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
  useDocumentTitle();
  const email =
    (user?.profile?.email as string | undefined) ??
    (user?.profile?.preferred_username as string | undefined) ??
    null;

  return (
    <AppShell email={email} onSignOut={onSignOut}>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/jobs" element={<Jobs />} />
        {/* Before this a job id was unlinkable: it appeared once, as mono text at the
            bottom of a card in a 25-row list. */}
        <Route path="/jobs/:id" element={<JobDetail />} />
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
