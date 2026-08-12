// Build-time public configuration, baked into the bundle by Vite.
// Nothing secret lives here — these are all values a browser would learn anyway.
//
// apiBase defaults to the page's own origin because the Ingress path-splits one
// hostname: the console is served from / and the API from /v1 on the same host,
// so the SPA needs no environment-specific URL at all in the normal deployment.

const fromEnv = (key: string, fallback: string): string => {
  const value = (import.meta.env as Record<string, string | undefined>)[key];
  return value && value.length > 0 ? value : fallback;
};

export const env = {
  apiBase: fromEnv("VITE_API_BASE", `${window.location.origin}/v1`),
  authAuthority: fromEnv(
    "VITE_AUTH_AUTHORITY",
    "https://auth.dicomsegvr.com/realms/dicomsegvr",
  ),
  kcClientId: fromEnv("VITE_KC_CLIENT_ID", "voxtell-console"),
} as const;
