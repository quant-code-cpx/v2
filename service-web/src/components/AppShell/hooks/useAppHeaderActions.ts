import { useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { MouseEvent } from "react";

import { useAuth } from "../../AuthProvider";
import { useFeedback } from "../../FeedbackProvider";

/** 管理应用头部菜单、预留动作反馈与显式退出。 */
export function useAppHeaderActions() {
  const { logout } = useAuth();
  const { info } = useFeedback();
  const navigate = useNavigate();
  const [accountMenuAnchor, setAccountMenuAnchor] = useState<HTMLElement | null>(null);

  /** 从已认证身份按钮打开账号菜单。 */
  const handleAccountMenuOpen = useCallback((event: MouseEvent<HTMLElement>) => {
    setAccountMenuAnchor(event.currentTarget);
  }, []);

  /** 关闭账号菜单，不改变路由或会话。 */
  const handleAccountMenuClose = useCallback(() => {
    setAccountMenuAnchor(null);
  }, []);

  /** 说明尚未冻结接口的预留全局搜索。 */
  const handleGlobalSearch = useCallback(() => {
    info("全局搜索功能即将开放。");
  }, [info]);

  /** 说明尚未完成的帮助入口。 */
  const handleHelp = useCallback(() => {
    info("帮助中心正在建设中。");
  }, [info]);

  /** 清理会话并用 React Router 替换为匿名登录路由。 */
  const handleLogout = useCallback(async () => {
    handleAccountMenuClose();
    await logout();
    info("已退出登录。");
    void navigate("/login", { replace: true });
  }, [handleAccountMenuClose, info, logout, navigate]);

  return {
    accountMenuAnchor,
    handleAccountMenuOpen,
    handleAccountMenuClose,
    handleGlobalSearch,
    handleHelp,
    handleLogout,
  };
}
