import { redirect } from "react-router-dom";
import type { LoaderFunctionArgs } from "react-router-dom";

import { authSession } from "../api/auth-session";
import { isApiError } from "../api/http";
import type { CurrentUser, Permission } from "../types/access";
import { safeReturnTo } from "../utils/return-to";

/** Redirect an anonymous visitor to login while preserving one safe in-app target. */
function loginRedirect(request: Request): Response {
  const requestUrl = new URL(request.url);
  const returnTo = `${requestUrl.pathname}${requestUrl.search}${requestUrl.hash}`;

  return redirect(`/login?returnTo=${encodeURIComponent(returnTo)}`);
}

/** Require a validated session before any protected route loader continues. */
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

/** Build a protected route loader that requires one server-calculated permission. */
export function requirePermission(permission: Permission) {
  /** Check the shared authenticated session before rendering a permission-bound route. */
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

/** Redirect an already authenticated visitor away from the sole anonymous login route. */
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
    // Login remains available when a refresh dependency is temporarily unavailable.
    return null;
  }
}
