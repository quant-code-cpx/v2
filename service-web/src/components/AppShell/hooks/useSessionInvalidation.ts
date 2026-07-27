import { useLayoutEffect, useState } from "react";

import { authSessionInvalidatedEvent } from "../../../api/auth-session";

/** 订阅终态会话失效事件，让壳层执行 React Router 站内跳转。 */
export function useSessionInvalidation(): boolean {
  const [sessionInvalidated, setSessionInvalidated] = useState(false);

  /** 壳层挂载期间订阅凭据终态失效，卸载时移除监听器。 */
  useLayoutEffect(() => {
    /** 标记会话失效，使匿名路由展示恢复指引。 */
    const handleSessionInvalidation = () => {
      setSessionInvalidated(true);
    };

    window.addEventListener(authSessionInvalidatedEvent, handleSessionInvalidation);

    return () => {
      window.removeEventListener(authSessionInvalidatedEvent, handleSessionInvalidation);
    };
  }, []);

  return sessionInvalidated;
}
