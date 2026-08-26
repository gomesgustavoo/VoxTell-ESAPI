import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const here = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  // The console is served at voxtell.dicomsegvr.com/dashboard/ so that `/` can be the
  // marketing landing page. This MUST be baked into the build: a Traefik StripPrefix
  // would leave the SPA emitting /assets/… , which resolves against the landing
  // service and 404s. Trailing slash is required — Vite treats a bare "/dashboard"
  // as a filename prefix rather than a directory.
  base: "/dashboard/",
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "dist",
    sourcemap: false,
    rollupOptions: {
      input: {
        // The SPA, plus the hidden iframe target used for OIDC silent renew.
        // A separate entry (rather than importing oidc-client-ts from a CDN in a
        // standalone html file) keeps the dependency local, which a hospital
        // network with no outbound internet needs.
        main: resolve(here, "index.html"),
        "silent-renew": resolve(here, "silent-renew.html"),
        // The preview harness, ONLY when explicitly asked for. Every page in this
        // app is behind the Keycloak gate, so without the harness there is no way
        // to look at Overview/Jobs/Keys/Billing at all — and reviewing them by
        // server-rendering markup in Node is what let the Tailwind @theme pruning
        // bug ship, because that path never runs Tailwind. The harness IS built
        // with the real index.css, so it does.
        //
        // Gated on an env var rather than on `mode`, because Dockerfile.console
        // does not set a mode and a production build must never emit this page.
        ...(process.env.VX_PREVIEW === "1"
          ? { __preview: resolve(here, "__preview.html") }
          : {}),
      },
    },
  },
});
