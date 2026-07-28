import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { useAuth } from "../../../components/AuthProvider";

/** 组合工作台权限视图与一次性并行刷新动作。 */
export function useWorkspace() {
  const { user, hasPermission } = useAuth();
  const queryClient = useQueryClient();

  /** 并行刷新当前权限可能拥有的账户、安全与管理摘要。 */
  const refresh = useCallback(async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["account"] }),
      queryClient.invalidateQueries({ queryKey: ["auth", "session-families"] }),
      queryClient.invalidateQueries({ queryKey: ["users", "statistics"] }),
      queryClient.invalidateQueries({ queryKey: ["audit-events"] }),
    ]);
  }, [queryClient]);

  return {
    user,
    canReadSessions: hasPermission("sessions:read"),
    canReadUsers: hasPermission("users:read"),
    canReadAudit: hasPermission("audit:read"),
    refresh,
  };
}
