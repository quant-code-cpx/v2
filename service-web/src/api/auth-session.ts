import { queryClient } from "./query-client";
import { getCurrentUser, loginWithCaptcha, logoutCurrentSession, refreshAccessToken } from "./auth";
import { ApiError, isApiError } from "./http";
import type { CurrentUser, LoginInput } from "../types/access";

/** 将当前用户状态保存在 TanStack Query，禁止写入浏览器持久存储。 */
export const authMeQueryKey = ["auth", "me"] as const;

/** 通知已挂载的受保护壳层：凭据已确认失效，需要执行站内安全跳转。 */
export const authSessionInvalidatedEvent = "apex-auth-session-invalidated";

/** 描述路由与 React Provider 可观察的非敏感会话状态。 */
export interface AuthSessionSnapshot {
  status: "checking" | "anonymous" | "authenticated";
}

/** 接收一次内存会话状态变更。 */
type SessionListener = (snapshot: AuthSessionSnapshot) => void;

/** 在契约规定的单次刷新重试前短暂等待。 */
function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, milliseconds);
  });
}

/** 在内存保存 access token，并协调路由与 UI 的单飞刷新。 */
class AuthSession {
  private accessToken: string | undefined;

  private snapshot: AuthSessionSnapshot = { status: "anonymous" };

  private refreshPromise: Promise<CurrentUser | null> | undefined;

  private readonly listeners = new Set<SessionListener>();

  /** 返回当前非敏感会话状态。 */
  public getSnapshot(): AuthSessionSnapshot {
    return this.snapshot;
  }

  /** 从权威 TanStack Query 缓存返回当前身份。 */
  public getCurrentUser(): CurrentUser | undefined {
    return queryClient.getQueryData<CurrentUser>(authMeQueryKey);
  }

  /** 返回 API 使用的 token，不向 React 状态或持久存储暴露。 */
  public getAccessToken(): string | undefined {
    return this.accessToken;
  }

  /** 订阅会话变更，并立即发布当前状态。 */
  public subscribe(listener: SessionListener): () => void {
    this.listeners.add(listener);
    listener(this.snapshot);

    /** React Provider 卸载时移除对应观察者。 */
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** 内存没有有效 token 时，启动或加入唯一刷新流程。 */
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

  /** 显式登录，仅保留内存 access token 与当前用户查询数据。 */
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

  /** 执行受保护请求；access token 失效时单飞刷新并只重放一次原请求。 */
  public async withAccessToken<T>(operation: (accessToken: string) => Promise<T>): Promise<T> {
    const accessToken = await this.accessTokenForRequest();

    try {
      return await operation(accessToken);
    } catch (error: unknown) {
      if (!isApiError(error) || error.status !== 401) {
        throw error;
      }

      const refreshedAccessToken = await this.refreshRejectedAccessToken(accessToken);

      try {
        return await operation(refreshedAccessToken);
      } catch (retryError: unknown) {
        // 新 token 仍被拒绝时禁止继续刷新，避免非幂等请求产生无限重放。
        if (isApiError(retryError) && retryError.status === 401) {
          this.clear(true);
        }

        throw retryError;
      }
    }
  }

  /** 显式退出后清理浏览器可见会话状态。 */
  public async logout(): Promise<void> {
    try {
      await logoutCurrentSession();
    } catch {
      // 服务端退出失败也必须清理客户端，避免历史记录恢复受保护内容。
    } finally {
      this.clear();
    }
  }

  /** 丢弃 token 与查询数据，并可通知已挂载壳层执行站内跳转。 */
  public clear(notifyProtectedShell = false): void {
    this.accessToken = undefined;
    queryClient.removeQueries({
      predicate: (query) => query.queryKey[0] === "auth" || query.queryKey[0] === "users",
    });
    // 即使本来已匿名也强制发布，避免已挂载壳层保留过期身份 UI。
    this.setSnapshot({ status: "anonymous" }, true);
    if (notifyProtectedShell && typeof window !== "undefined") {
      window.dispatchEvent(new Event(authSessionInvalidatedEvent));
    }
  }

  /** 状态变化或强制凭据失效后通知所有观察者。 */
  private setSnapshot(nextSnapshot: AuthSessionSnapshot, forcePublish = false): void {
    if (this.snapshot.status === nextSnapshot.status && !forcePublish) {
      return;
    }

    this.snapshot = nextSnapshot;

    /** 向每个观察者发布仅包含安全状态的快照。 */
    this.listeners.forEach((listener) => {
      listener(this.snapshot);
    });
  }

  /** 为请求取得 access token；冷启动时复用会话恢复，不提前卸载受保护页面。 */
  private async accessTokenForRequest(): Promise<string> {
    if (this.accessToken !== undefined) {
      return this.accessToken;
    }

    const user = await this.ensureSession();
    if (user === null || this.accessToken === undefined) {
      throw new ApiError(401, "unauthorized");
    }

    return this.accessToken;
  }

  /** 首次业务 401 后刷新；并发请求复用同一 Promise，已更新 token 则直接使用。 */
  private async refreshRejectedAccessToken(rejectedAccessToken: string): Promise<string> {
    if (this.accessToken !== undefined && this.accessToken !== rejectedAccessToken) {
      return this.accessToken;
    }

    if (this.refreshPromise === undefined) {
      const previousSnapshot = this.snapshot;
      const refreshPromise = this.refreshSession(previousSnapshot);
      this.refreshPromise = refreshPromise;

      try {
        await refreshPromise;
      } finally {
        if (this.refreshPromise === refreshPromise) {
          this.refreshPromise = undefined;
        }
      }
    } else {
      await this.refreshPromise;
    }

    if (this.accessToken === undefined) {
      throw new ApiError(401, "unauthorized");
    }

    return this.accessToken;
  }

  /** 执行一次 refresh；除服务端返回 401 外均保留既有身份。 */
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

  /** 仅清理被确认拒绝的凭据；权限或依赖故障保留既有身份。 */
  private handleRefreshFailure(
    error: unknown,
    previousSnapshot: AuthSessionSnapshot,
  ): CurrentUser | null {
    if (isApiError(error) && error.status === 401) {
      this.clear(true);
      return null;
    }

    // 403 是权限结果，不代表已认证会话失效。
    this.setSnapshot(previousSnapshot);
    throw error;
  }

  /** 通过 GET /users/me 验证 token 后，才向路由或 UI 暴露身份。 */
  private async acceptAccessToken(
    accessToken: string,
    clearOnIdentityFailure = true,
  ): Promise<CurrentUser> {
    const previousAccessToken = this.accessToken;
    const previousUser = this.getCurrentUser();
    this.accessToken = accessToken;

    try {
      const user = await queryClient.fetchQuery({
        queryKey: authMeQueryKey,
        queryFn: () => getCurrentUser(accessToken),
        // 后台刷新期间保留旧身份；本次调用仍强制向服务端验证新 token。
        staleTime: 0,
      });
      this.setSnapshot({ status: "authenticated" });
      return user;
    } catch (error: unknown) {
      if (clearOnIdentityFailure || (isApiError(error) && error.status === 401)) {
        this.clear(!clearOnIdentityFailure);
      } else {
        // 刷新验证失败不能把既有已授权 UI 误变成匿名状态。
        this.accessToken = previousAccessToken;
        if (previousUser !== undefined) {
          queryClient.setQueryData(authMeQueryKey, previousUser);
        }
      }
      throw error;
    }
  }
}

/** 导出由路由 loader 与 React 共享的进程内会话协调器。 */
export const authSession = new AuthSession();
