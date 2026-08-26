// The app shell: sidebar, topbar, ambient field.
//
// Cloned in shape from dicomsegvr/dashboard's AppShell, so a customer who has both
// products sees one interface rather than two. Same 44px workstation grid masked at
// the top, same radial field, same sidebar-with-accent-marker treatment — with the
// cyan ramp instead of the rose one.

import { NavLink } from "react-router-dom";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/jobs", label: "Jobs", end: false },
  { to: "/keys", label: "Keys", end: false },
  { to: "/billing", label: "Billing", end: false },
];

export function Wordmark({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-2 ${className}`}>
      {/* Three stacked slices and a contour: the mark is a piece of what the
          product makes, the same idea as the favicon. */}
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden>
        <path
          d="M3 8.5 12 4l9 4.5-9 4.5-9-4.5Z"
          stroke="var(--color-accent)"
          strokeWidth="1.6"
          strokeLinejoin="round"
        />
        <path
          d="M3 14 12 18.5 21 14"
          stroke="var(--color-accent-2)"
          strokeWidth="1.6"
          strokeLinejoin="round"
        />
      </svg>
      <span className="text-md font-bold tracking-[-0.02em] text-ink">
        Vox<span className="text-grad">Tell</span>
      </span>
    </span>
  );
}

function NavItems({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <>
      {NAV.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.end}
          onClick={onNavigate}
          className={({ isActive }) =>
            "relative rounded-chip px-3 py-2 font-mono text-xs uppercase tracking-label transition-colors " +
            (isActive
              ? "bg-accent/10 text-accent before:absolute before:top-1/2 before:left-0 before:h-5 before:w-0.5 before:-translate-y-1/2 before:rounded-pill before:bg-grad-brand"
              : "text-muted hover:bg-surface-2 hover:text-ink")
          }
        >
          {item.label}
        </NavLink>
      ))}
    </>
  );
}

export function AppShell({
  email,
  onSignOut,
  children,
}: {
  email: string | null;
  onSignOut: () => void;
  children: ReactNode;
}) {
  const [menuOpen, setMenuOpen] = useState(false);

  // Close the drawer on Escape, matching the landing page's nav.
  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setMenuOpen(false);
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  return (
    <div className="relative min-h-full">
      {/* Ambient field + workstation grid. Fixed and pointer-events:none so it can
          never intercept a click, and masked at the top so the grid fades out
          rather than ending on a hard line. */}
      <div className="pointer-events-none fixed inset-0 -z-10 bg-mesh" aria-hidden />
      <div
        className="pointer-events-none fixed inset-0 -z-10 opacity-[0.5]"
        aria-hidden
        style={{
          backgroundImage:
            "linear-gradient(rgba(147,161,184,.05) 1px, transparent 1px)," +
            "linear-gradient(90deg, rgba(147,161,184,.05) 1px, transparent 1px)",
          backgroundSize: "44px 44px",
          maskImage: "radial-gradient(120% 80% at 50% 0%, #000 25%, transparent 75%)",
          WebkitMaskImage: "radial-gradient(120% 80% at 50% 0%, #000 25%, transparent 75%)",
        }}
      />

      <div className="mx-auto flex w-full max-w-[1180px] gap-8 px-4 sm:px-6">
        {/* Sidebar, desktop only. */}
        <aside className="sticky top-0 hidden h-dvh w-48 flex-none flex-col py-6 lg:flex">
          <NavLink to="/" className="mb-8 px-3">
            <Wordmark />
          </NavLink>
          <nav className="flex flex-col gap-1" aria-label="Sections">
            <NavItems />
          </nav>
          <div className="mt-auto px-3">
            <p className="truncate font-mono text-[10px] text-faint" title={email ?? undefined}>
              {email ?? "signed in"}
            </p>
            <button
              onClick={onSignOut}
              className="mt-2 font-mono text-[10px] uppercase tracking-label text-muted hover:text-ink"
            >
              Sign out
            </button>
          </div>
        </aside>

        <div className="min-w-0 flex-1 py-6">
          {/* Topbar, mobile only. */}
          <header className="mb-6 flex items-center justify-between gap-3 lg:hidden">
            <NavLink to="/">
              <Wordmark />
            </NavLink>
            <button
              onClick={() => setMenuOpen((v) => !v)}
              aria-expanded={menuOpen}
              aria-label="Menu"
              className="flex h-10 w-10 items-center justify-center rounded-chip border border-border text-ink"
            >
              <svg width="18" height="14" viewBox="0 0 18 14" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
                <path d="M1 1h16M1 7h16M1 13h16" />
              </svg>
            </button>
          </header>

          {menuOpen && (
            <nav className="mb-6 flex flex-col gap-1 rounded-card border border-border bg-surface p-2 lg:hidden" aria-label="Sections">
              <NavItems onNavigate={() => setMenuOpen(false)} />
              <button
                onClick={onSignOut}
                className="mt-1 rounded-chip px-3 py-2 text-left font-mono text-xs uppercase tracking-label text-muted hover:bg-surface-2 hover:text-ink"
              >
                Sign out
              </button>
            </nav>
          )}

          <main>{children}</main>

          <footer className="mt-12 border-t border-border pt-5 text-xs leading-relaxed text-faint">
            Segmentation output is for research and planning support — always review
            contours before clinical use. VoxTell model by MIC, DKFZ Heidelberg
            (arXiv:2511.11450). Not a medical device.
          </footer>
        </div>
      </div>
    </div>
  );
}

/**
 * Centred single-panel layout for sign-in, callback and errors.
 *
 * `bare` omits the mesh. The unauthenticated screens render OUTSIDE AppShell and
 * need their own background; the 404 route renders INSIDE it, and mounting a second
 * fixed mesh layer there stacked the two — that route was visibly lighter than every
 * other page in the app, which reads as a rendering fault rather than a design.
 */
export function Centered({ children, bare = false }: { children: ReactNode; bare?: boolean }) {
  return (
    <div className="relative flex min-h-full items-center justify-center px-4 py-16">
      {!bare && <div className="pointer-events-none fixed inset-0 -z-10 bg-mesh" aria-hidden />}
      <div className="w-full max-w-sm text-center">{children}</div>
    </div>
  );
}
