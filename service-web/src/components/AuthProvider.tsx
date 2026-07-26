import { useQuery } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useSyncExternalStore,
} from "react";
import type { PropsWithChildren } from "react";

import { authMeQueryKey, authSession } from "../api/auth-session";
import type { AuthSessionSnapshot } from "../api/auth-session";
import type { CurrentUser, LoginInput, Permission } from "../types/access";

/** Describe non-secret session state and actions consumable by application components. */
interface AuthContextValue {
  status: AuthSessionSnapshot["status"];
  user: CurrentUser | undefined;
  login: (input: LoginInput) => Promise<CurrentUser>;
  logout: () => Promise<void>;
  hasPermission: (permission: Permission) => boolean;
}

/** Keep auth unavailable outside its root provider so protected UI cannot silently degrade. */
const AuthContext = createContext<AuthContextValue | undefined>(undefined);

/** Subscribe React's external-store bridge to safe status-only auth-session transitions. */
function subscribeToAuthSession(listener: () => void): () => void {
  return authSession.subscribe(listener);
}

/** Read the stable status snapshot held by the single process-local auth-session coordinator. */
function readAuthSessionSnapshot(): AuthSessionSnapshot {
  return authSession.getSnapshot();
}

/** Render session-aware descendants and subscribe them to the in-memory auth coordinator. */
export function AuthProvider({ children }: PropsWithChildren) {
  const snapshot = useSyncExternalStore(
    subscribeToAuthSession,
    readAuthSessionSnapshot,
    readAuthSessionSnapshot,
  );
  const currentUserQuery = useQuery({
    queryKey: authMeQueryKey,
    queryFn: async () => {
      const user = authSession.getCurrentUser();
      if (user === undefined) {
        throw new Error("Authenticated user is unavailable.");
      }

      return user;
    },
    enabled: snapshot.status === "authenticated",
    staleTime: Number.POSITIVE_INFINITY,
  });

  /** Start initial refresh once; useSyncExternalStore keeps status current for every later transition. */
  useEffect(() => {
    void authSession.ensureSession().catch(() => {
      // Route loaders expose unavailable auth dependencies through their error boundary.
    });
  }, []);

  /** Delegate explicit login to the session coordinator. */
  const login = useCallback(async (input: LoginInput) => authSession.login(input), []);

  /** Delegate explicit logout to the session coordinator. */
  const logout = useCallback(async () => authSession.logout(), []);

  /** Check server-calculated permissions without inventing client-side authorization. */
  const hasPermission = useCallback(
    (permission: Permission) => currentUserQuery.data?.permissions.includes(permission) ?? false,
    [currentUserQuery.data],
  );

  /** Keep context identity stable between session and identity updates. */
  const value = useMemo<AuthContextValue>(
    () => ({
      status: snapshot.status,
      user: currentUserQuery.data,
      login,
      logout,
      hasPermission,
    }),
    [currentUserQuery.data, hasPermission, login, logout, snapshot.status],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** Read session context and fail loudly if a protected component is mounted without it. */
export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);

  if (context === undefined) {
    throw new Error("useAuth must be used inside AuthProvider.");
  }

  return context;
}
