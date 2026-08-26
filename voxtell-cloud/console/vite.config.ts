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
      },
    },
  },
});
