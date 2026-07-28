import { redirect } from "react-router-dom";
import type { LoaderFunctionArgs } from "react-router-dom";

import { authSession } from "../api/auth-session";
import { isApiError } from "../api/http";
import type { CurrentUser, Permission } from "../types/access";
import { safeReturnTo } from "../utils/return-to";

/** 将匿名访问者重定向到登录，并保留一个安全站内目标。 */
function loginRedirect(request: Request): Response {
  const requestUrl = new URL(request.url);
  const returnTo = `${requestUrl.pathname}${requestUrl.search}${requestUrl.hash}`;

  return redirect(`/login?returnTo=${encodeURIComponent(returnTo)}`);
}

/** 在任何受保护路由 loader 继续前要求已验证会话。 */
export async function requireSession({
  request,
}: LoaderFunctionArgs): Promise<CurrentUser | Response> {
  try {
    const user = await authSession.ensureSession();

    if (user === null) {
      return loginRedirect(request);
    }

    return user;
  } catch (error: unknown) {
    if (isApiError(error) && error.status === 403) {
      throw new Response("无权访问此功能。", { status: 403, statusText: "Forbidden" });
    }

    throw new Response("认证服务暂时不可用。", { status: 503, statusText: "Service Unavailable" });
  }
}

/** 构造要求一个服务端计算权限的受保护路由 loader。 */
export function requirePermission(permission: Permission) {
  /** 在渲染权限路由前检查共享已认证会话。 */
  return async (arguments_: LoaderFunctionArgs): Promise<CurrentUser | Response> => {
    const userOrRedirect = await requireSession(arguments_);

    if (userOrRedirect instanceof Response) {
      return userOrRedirect;
    }

    if (!userOrRedirect.permissions.includes(permission)) {
      throw new Response("无权访问此功能。", { status: 403, statusText: "Forbidden" });
    }

    return userOrRedirect;
  };
}

/** 将已认证访问者从唯一匿名登录路由重定向出去。 */
export async function redirectAuthenticatedLogin({
  request,
}: LoaderFunctionArgs): Promise<Response | null> {
  try {
    const user = await authSession.ensureSession();
    if (user === null) {
      return null;
    }

    const requestUrl = new URL(request.url);
    return redirect(safeReturnTo(requestUrl.searchParams.get("returnTo")));
  } catch {
    // refresh 依赖暂时不可用时仍保留登录入口。
    return null;
  }
}
