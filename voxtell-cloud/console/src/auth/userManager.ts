import {
  UserManager,
  WebStorageStateStore,
  type UserManagerSettings,
} from "oidc-client-ts";
import { env } from "../lib/env";

export const oidcSettings: UserManagerSettings = {
  authority: env.authAuthority,
  client_id: env.kcClientId,
  redirect_uri: `${window.location.origin}/auth/callback`,
  silent_redirect_uri: `${window.location.origin}/silent-renew.html`,
  post_logout_redirect_uri: `${window.location.origin}/`,
  response_type: "code", // Authorization Code + PKCE (automatic for a public client)
  scope: "openid profile email",
  automaticSilentRenew: true,
  accessTokenExpiringNotificationTimeInSeconds: 60,
  includeIdTokenInSilentRenew: true,
  // Keycloak's session-check iframe needs third-party cookies, which do not
  // survive Cloudflare + SameSite. Silent renew covers session freshness.
  monitorSession: false,
  // Session storage, not local: tokens die with the tab. This console mints API
  // keys, so a shorter-lived credential in the browser is the right trade.
  userStore: new WebStorageStateStore({ store: window.sessionStorage }),
  stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
};

export const userManager = new UserManager(oidcSettings);

userManager.events.addSilentRenewError((err) => {
  console.warn("[oidc] silent renew error:", err);
});
