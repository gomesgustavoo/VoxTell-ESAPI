import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";

import { AuthProvider } from "./auth/AuthProvider";
import { App } from "./App";
import "./index.css";

// basename must match Vite's `base` and nginx's location block. All three are
// /dashboard/ so that `/` on this hostname can be the marketing landing page; change
// one and you must change all three or the SPA 404s on every route but the root.
const BASENAME = "/dashboard";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Traefik rate-limits /v1 (average 100/s, burst 200, keyed on
      // Cf-Connecting-IP), so refetching on every window focus is a poor trade for a
      // dashboard that already polls where it matters. Individual queries opt into
      // refetchInterval; nothing here polls by default.
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 30_000,
    },
  },
});

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("Root element #root not found");

createRoot(rootEl).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <BrowserRouter basename={BASENAME}>
          <App />
        </BrowserRouter>
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
);
