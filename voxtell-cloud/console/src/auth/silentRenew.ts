// Entry point for silent-renew.html, loaded in the hidden OIDC iframe.
// It must construct the UserManager from the SAME settings as the app so the
// PKCE state store matches; a mismatched store is the classic "state not found"
// silent-renew failure.
import { UserManager } from "oidc-client-ts";
import { oidcSettings } from "./userManager";

new UserManager(oidcSettings)
  .signinSilentCallback()
  .catch((err) => console.error("[oidc] silent renew callback error:", err));
