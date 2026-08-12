import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { User } from "oidc-client-ts";
import { userManager } from "./userManager";

export interface AuthContextValue {
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  isSigningOut: boolean;
  error: Error | null;
  token: string | null;
  signin: () => Promise<void>;
  signout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>");
  return ctx;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  // Sign-out unloads the user before the browser reaches Keycloak's end-session
  // endpoint. Without this flag the "not authenticated" effect fires in that gap
  // and starts a fresh sign-in, which aborts the navigation and surfaces as
  // "Code not valid". (This bites the DicomSegVR dashboard; do not inherit it.)
  const signingOut = useRef(false);
  const [isSigningOut, setIsSigningOut] = useState(false);

  useEffect(() => {
    let active = true;

    userManager
      .getUser()
      .then((u) => {
        if (active) setUser(u && !u.expired ? u : null);
      })
      .catch((e: unknown) => {
        if (active) setError(e instanceof Error ? e : new Error(String(e)));
      })
      .finally(() => {
        if (active) setIsLoading(false);
      });

    const onUserLoaded = (u: User) => setUser(u);
    const onUserUnloaded = () => setUser(null);
    const onAccessTokenExpired = () => {
      setUser(null);
      if (signingOut.current) return;
      void userManager.signinRedirect().catch(() => undefined);
    };

    userManager.events.addUserLoaded(onUserLoaded);
    userManager.events.addUserUnloaded(onUserUnloaded);
    userManager.events.addAccessTokenExpired(onAccessTokenExpired);
    return () => {
      active = false;
      userManager.events.removeUserLoaded(onUserLoaded);
      userManager.events.removeUserUnloaded(onUserUnloaded);
      userManager.events.removeAccessTokenExpired(onAccessTokenExpired);
    };
  }, []);

  const signin = useCallback(async () => {
    await userManager.signinRedirect();
  }, []);

  const signout = useCallback(async () => {
    signingOut.current = true;
    setIsSigningOut(true);
    try {
      await userManager.signoutRedirect();
    } catch (e) {
      signingOut.current = false;
      setIsSigningOut(false);
      throw e;
    }
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      isAuthenticated: !!user && !user.expired,
      isLoading,
      isSigningOut,
      error,
      token: user?.access_token ?? null,
      signin,
      signout,
    }),
    [user, isLoading, isSigningOut, error, signin, signout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
