import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const here = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
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
