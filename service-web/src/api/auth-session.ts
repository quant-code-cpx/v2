import { queryClient } from "./query-client";
import { getCurrentUser, loginWithCaptcha, logoutCurrentSession, refreshAccessToken } from "./auth";
import { ApiError, isApiError } from "./http";
import type { CurrentUser, LoginInput } from "../types/access";

/** Keep current-user state in TanStack Query rather than browser persistence. */
export const authMeQueryKey = ["auth", "me"] as const;

/** Signal a mounted protected shell that a credential rejection requires an immediate safe redirect. */
export const authSessionInvalidatedEvent = "apex-auth-session-invalidated";

/** Describe non-secret session state observed by routes and React providers. */
export interface AuthSessionSnapshot {
  status: "checking" | "anonymous" | "authenticated";
}

/** Receive an in-memory session state change. */
type SessionListener = (snapshot: AuthSessionSnapshot) => void;

/** Pause briefly before the contract-defined single refresh retry. */
function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, milliseconds);
  });
}

/** Keep access tokens in memory and coordinate refreshes for routes and UI. */
class AuthSession {
  private accessToken: string | undefined;

  private snapshot: AuthSessionSnapshot = { status: "anonymous" };

  private refreshPromise: Promise<CurrentUser | null> | undefined;

  private readonly listeners = new Set<SessionListener>();

  /** Return current non-secret session status. */
  public getSnapshot(): AuthSessionSnapshot {
    return this.snapshot;
  }

  /** Return current identity from the authoritative TanStack Query cache. */
  public getCurrentUser(): CurrentUser | undefined {
    return queryClient.getQueryData<CurrentUser>(authMeQueryKey);
  }

  /** Return token for API use without exposing it to React or persistent storage. */
  public getAccessToken(): string | undefined {
    return this.accessToken;
  }

  /** Subscribe UI to session transitions and immediately publish current status. */
  public subscribe(listener: SessionListener): () => void {
    this.listeners.add(listener);
    listener(this.snapshot);

    /** Remove one observer when its React provider unmounts. */
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** Start or join a single refresh flow when no valid in-memory token exists. */
  public async ensureSession(): Promise<CurrentUser | null> {
    const cachedUser = this.getCurrentUser();
    if (this.snapshot.status === "authenticated" && cachedUser !== undefined) {
      return cachedUser;
    }

    if (this.refreshPromise !== undefined) {
      return this.refreshPromise;
    }

    const previousSnapshot = this.snapshot;
    this.setSnapshot({ status: "checking" });
    this.refreshPromise = this.refreshSession(previousSnapshot);

    try {
      return await this.refreshPromise;
    } finally {
      this.refreshPromise = undefined;
    }
  }

  /** Authenticate explicitly, retaining only access token memory and current-user query data. */
  public async login(input: LoginInput): Promise<CurrentUser> {
    this.setSnapshot({ status: "checking" });

    try {
      const response = await loginWithCaptcha(input);
      return await this.acceptAccessToken(response.accessToken);
    } catch (error: unknown) {
      this.clear();
      throw error;
    }
  }

  /** Execute one protected request and clear in-memory state on a rejected session. */
  public async withAccessToken<T>(operation: (accessToken: string) => Promise<T>): Promise<T> {
    const accessToken = this.accessToken;
    if (accessToken === undefined) {
      // A protected request without a process-local token cannot safely retain protected UI state.
      this.clear(true);
      throw new ApiError(401, "unauthorized");
    }

    try {
      return await operation(accessToken);
    } catch (error: unknown) {
      if (isApiError(error) && error.status === 401) {
        this.clear(true);
      }

      throw error;
    }
  }

  /** Clear browser-visible session state after an explicit logout attempt. */
  public async logout(): Promise<void> {
    try {
      await logoutCurrentSession();
    } catch {
      // Client state must still clear so browser history cannot restore protected content.
    } finally {
      this.clear();
    }
  }

  /** Drop token and query data, optionally notifying an already-mounted protected shell to redirect. */
  public clear(notifyProtectedShell = false): void {
    this.accessToken = undefined;
    queryClient.removeQueries({
      predicate: (query) => query.queryKey[0] === "auth" || query.queryKey[0] === "users",
    });
    // Publish even when already anonymous so mounted protected shells cannot retain stale identity UI.
    this.setSnapshot({ status: "anonymous" }, true);
    if (notifyProtectedShell && typeof window !== "undefined") {
      window.dispatchEvent(new Event(authSessionInvalidatedEvent));
      this.replaceToLogin();
    }
  }

  /** Notify listeners after status transitions, or a forced credential/cache invalidation. */
  private setSnapshot(nextSnapshot: AuthSessionSnapshot, forcePublish = false): void {
    if (this.snapshot.status === nextSnapshot.status && !forcePublish) {
      return;
    }

    this.snapshot = nextSnapshot;

    /** Publish a safe status-only snapshot to each current observer. */
    this.listeners.forEach((listener) => {
      listener(this.snapshot);
    });
  }

  /** Replace browser history from a terminal protected-request 401 before restricted UI can persist. */
  private replaceToLogin(): void {
    if (import.meta.env.MODE === "test" || window.location.pathname === "/login") {
      return;
    }

    const returnTo = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    window.location.replace(
      `/login?returnTo=${encodeURIComponent(returnTo)}&reason=session-expired`,
    );
  }

  /** Perform refresh once, retaining an established identity unless the server returns 401. */
  private async refreshSession(previousSnapshot: AuthSessionSnapshot): Promise<CurrentUser | null> {
    try {
      const response = await refreshAccessToken();
      return await this.acceptAccessToken(response.accessToken, false);
    } catch (error: unknown) {
      if (isApiError(error) && error.status === 409) {
        await delay((error.retryAfterSeconds ?? 1) * 1_000);

        try {
          const response = await refreshAccessToken();
          return await this.acceptAccessToken(response.accessToken, false);
        } catch (retryError: unknown) {
          return this.handleRefreshFailure(retryError, previousSnapshot);
        }
      }

      return this.handleRefreshFailure(error, previousSnapshot);
    }
  }

  /** Clear only rejected credentials; keep a prior identity for permission or dependency failures. */
  private handleRefreshFailure(
    error: unknown,
    previousSnapshot: AuthSessionSnapshot,
  ): CurrentUser | null {
    if (isApiError(error) && error.status === 401) {
      this.clear(true);
      return null;
    }

    // A 403 is a permission result, not evidence that the authenticated session is invalid.
    this.setSnapshot(previousSnapshot);
    throw error;
  }

  /** Verify a token with GET /users/me before exposing identity to routes or UI. */
  private async acceptAccessToken(
    accessToken: string,
    clearOnIdentityFailure = true,
  ): Promise<CurrentUser> {
    const previousAccessToken = this.accessToken;
    const previousUser = this.getCurrentUser();
    this.accessToken = accessToken;
    queryClient.removeQueries({ queryKey: authMeQueryKey, exact: true });

    try {
      const user = await queryClient.fetchQuery({
        queryKey: authMeQueryKey,
        queryFn: () => getCurrentUser(accessToken),
        staleTime: Number.POSITIVE_INFINITY,
      });
      this.setSnapshot({ status: "authenticated" });
      return user;
    } catch (error: unknown) {
      if (clearOnIdentityFailure || (isApiError(error) && error.status === 401)) {
        this.clear(!clearOnIdentityFailure);
      } else {
        // A failed refresh verification must not turn a prior authorized UI into anonymous state.
        this.accessToken = previousAccessToken;
        if (previousUser !== undefined) {
          queryClient.setQueryData(authMeQueryKey, previousUser);
        }
      }
      throw error;
    }
  }
}

/** Export one process-local session coordinator shared by loaders and React. */
export const authSession = new AuthSession();
