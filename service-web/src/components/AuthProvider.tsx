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

/** 描述应用组件可消费的非敏感会话状态与动作。 */
interface AuthContextValue {
  status: AuthSessionSnapshot["status"];
  user: CurrentUser | undefined;
  login: (input: LoginInput) => Promise<CurrentUser>;
  logout: () => Promise<void>;
  hasPermission: (permission: Permission) => boolean;
}

/** Provider 外不提供鉴权上下文，避免受保护 UI 静默降级。 */
const AuthContext = createContext<AuthContextValue | undefined>(undefined);

/** 将 React 外部存储桥接到仅含安全状态的会话变化。 */
function subscribeToAuthSession(listener: () => void): () => void {
  return authSession.subscribe(listener);
}

/** 读取进程内唯一会话协调器持有的稳定状态快照。 */
function readAuthSessionSnapshot(): AuthSessionSnapshot {
  return authSession.getSnapshot();
}

/** 渲染会话感知的后代，并订阅内存鉴权协调器。 */
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

  /** 首次挂载只触发一次恢复，后续状态由 useSyncExternalStore 保持同步。 */
  useEffect(() => {
    void authSession.ensureSession().catch(() => {
      // 路由 loader 通过错误边界呈现不可用的鉴权依赖。
    });
  }, []);

  /** 将显式登录委托给会话协调器。 */
  const login = useCallback(async (input: LoginInput) => authSession.login(input), []);

  /** 将显式退出委托给会话协调器。 */
  const logout = useCallback(async () => authSession.logout(), []);

  /** 仅检查服务端计算的权限，不在客户端臆造授权。 */
  const hasPermission = useCallback(
    (permission: Permission) => currentUserQuery.data?.permissions.includes(permission) ?? false,
    [currentUserQuery.data],
  );

  /** 在会话与身份未变化时保持上下文引用稳定。 */
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

/** 读取会话上下文；受保护组件未包裹 Provider 时立即失败。 */
export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);

  if (context === undefined) {
    throw new Error("useAuth must be used inside AuthProvider.");
  }

  return context;
}
