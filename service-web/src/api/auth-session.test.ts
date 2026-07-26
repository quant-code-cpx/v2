import { afterEach, beforeEach, describe, expect, it } from "vite-plus/test";

import { createLoginCaptcha } from "./auth";
import { authMeQueryKey, authSession } from "./auth-session";
import { queryClient } from "./query-client";
import { ApiError, setHttpTransportForTests } from "./http";
import { listUsers } from "./users";
import type { HttpTransportRequest } from "./http";

/** Build a contract-shaped JSON response for a controlled transport test. */
function jsonResponse(status: number, body: unknown, headers: HeadersInit = {}): Response {
  const responseHeaders = new Headers(headers);
  responseHeaders.set("Content-Type", "application/json");

  return new Response(JSON.stringify(body), {
    status,
    headers: responseHeaders,
  });
}

/** Return a current-user payload with no credentials, tokens, or CAPTCHA secret. */
function currentUserPayload() {
  return {
    id: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
    account: "admin.demo",
    displayName: "管理员",
    role: "ADMIN",
    status: "ACTIVE",
    version: 1,
    lastLoginAt: null,
    deletedAt: null,
    createdAt: "2026-07-26T00:00:00.000Z",
    updatedAt: "2026-07-26T00:00:00.000Z",
    permissions: [
      "users:read",
      "users:create",
      "users:update",
      "users:delete",
      "users:reset-password",
    ],
  };
}

describe("auth session", () => {
  /** Reset global process-local auth and transport state between isolated unit tests. */
  beforeEach(() => {
    authSession.clear();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  /** Restore production transport after each controlled test adapter. */
  afterEach(() => {
    setHttpTransportForTests();
    authSession.clear();
  });

  /** Keep access token only in memory while fetching identity into the auth query cache. */
  it("stores access token in memory and current user in TanStack Query", async () => {
    const requests: HttpTransportRequest[] = [];
    setHttpTransportForTests(async (request) => {
      requests.push(request);
      if (request.url.endsWith("/api/v1/auth/login")) {
        return jsonResponse(200, {
          accessToken: "short-lived-token",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }

      return jsonResponse(200, currentUserPayload());
    });

    const user = await authSession.login({
      account: "admin.demo",
      password: "secure-pass-123",
      captchaId: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
      captchaAnswer: "1234",
    });

    expect(user.account).toBe("admin.demo");
    expect(authSession.getAccessToken()).toBe("short-lived-token");
    expect(queryClient.getQueryData(authMeQueryKey)).toEqual(currentUserPayload());
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(requests.every((request) => request.init.credentials === "include")).toBe(true);
    expect(new Headers(requests[1]?.init.headers).get("Authorization")).toBe(
      "Bearer short-lived-token",
    );
  });

  /** Join concurrent session restoration requests rather than rotating one refresh cookie twice. */
  it("uses one refresh request for concurrent session restoration", async () => {
    let refreshCalls = 0;
    setHttpTransportForTests(async (request) => {
      if (request.url.endsWith("/api/v1/auth/refresh")) {
        refreshCalls += 1;
        return jsonResponse(200, {
          accessToken: "refreshed-token",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }

      return jsonResponse(200, currentUserPayload());
    });

    const [firstUser, secondUser] = await Promise.all([
      authSession.ensureSession(),
      authSession.ensureSession(),
    ]);

    expect(firstUser?.id).toBe(secondUser?.id);
    expect(refreshCalls).toBe(1);
  });

  /** Treat refresh 401 as the sole terminal session failure and erase protected client state. */
  it("clears identity only when refresh rejects credentials with 401", async () => {
    setHttpTransportForTests(async () => jsonResponse(401, { code: "invalid-refresh-token" }));

    await expect(authSession.ensureSession()).resolves.toBeNull();

    expect(authSession.getAccessToken()).toBeUndefined();
    expect(queryClient.getQueryData(authMeQueryKey)).toBeUndefined();
    expect(authSession.getSnapshot().status).toBe("anonymous");
  });

  /** Surface refresh 403 without discarding non-secret identity and user-cache state. */
  it("reports refresh 403 while retaining cached identity", async () => {
    const retainedUser = currentUserPayload();
    queryClient.setQueryData(authMeQueryKey, retainedUser);
    queryClient.setQueryData(["users", "list"], { items: [] });
    setHttpTransportForTests(async () => jsonResponse(403, { code: "forbidden" }));

    await expect(authSession.ensureSession()).rejects.toMatchObject({ status: 403 });

    expect(authSession.getAccessToken()).toBeUndefined();
    expect(queryClient.getQueryData(authMeQueryKey)).toEqual(retainedUser);
    expect(queryClient.getQueryData(["users", "list"])).toEqual({ items: [] });
    expect(authSession.getSnapshot().status).toBe("anonymous");
  });

  /** Keep an already authenticated identity when a protected action is forbidden, not unauthenticated. */
  it("retains an established identity after a protected 403", async () => {
    setHttpTransportForTests(async (request) => {
      if (request.url.endsWith("/api/v1/auth/login")) {
        return jsonResponse(200, {
          accessToken: "short-lived-token",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }

      return jsonResponse(200, currentUserPayload());
    });
    await authSession.login({
      account: "admin.demo",
      password: "secure-pass-123",
      captchaId: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
      captchaAnswer: "1234",
    });

    await expect(
      authSession.withAccessToken(async () => {
        throw new ApiError(403, "forbidden");
      }),
    ).rejects.toMatchObject({ status: 403 });

    expect(authSession.getAccessToken()).toBe("short-lived-token");
    expect(queryClient.getQueryData(authMeQueryKey)).toEqual(currentUserPayload());
    expect(authSession.getSnapshot().status).toBe("authenticated");
  });

  /** Clear an expired protected HTTP session so the shell can safely replace to the login route. */
  it("clears identity after a protected API 401 response", async () => {
    setHttpTransportForTests(async (request) => {
      if (request.url.endsWith("/api/v1/auth/login")) {
        return jsonResponse(200, {
          accessToken: "short-lived-token",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (request.url.includes("/api/v1/users?")) {
        return jsonResponse(401, { code: "access-token-expired" });
      }

      return jsonResponse(200, currentUserPayload());
    });
    await authSession.login({
      account: "admin.demo",
      password: "secure-pass-123",
      captchaId: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
      captchaAnswer: "1234",
    });

    await expect(
      listUsers({ sort: "createdAt", order: "desc", pageSize: 20 }),
    ).rejects.toMatchObject({ status: 401 });

    expect(authSession.getAccessToken()).toBeUndefined();
    expect(queryClient.getQueryData(authMeQueryKey)).toBeUndefined();
    expect(authSession.getSnapshot().status).toBe("anonymous");
  });

  /** Include the stable initial table order in every list request. */
  it("sends the default created-time descending list order", async () => {
    let userListRequest: HttpTransportRequest | undefined;
    setHttpTransportForTests(async (request) => {
      if (request.url.endsWith("/api/v1/auth/login")) {
        return jsonResponse(200, {
          accessToken: "short-lived-token",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (request.url.includes("/api/v1/users?")) {
        userListRequest = request;
        return jsonResponse(200, { items: [], page: { nextCursor: null } });
      }

      return jsonResponse(200, currentUserPayload());
    });
    await authSession.login({
      account: "admin.demo",
      password: "secure-pass-123",
      captchaId: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
      captchaAnswer: "1234",
    });

    await listUsers({ sort: "createdAt", order: "desc", pageSize: 20 });

    const query = new URL(userListRequest?.url ?? "/", "http://apex.local").searchParams;
    expect(query.get("pageSize")).toBe("20");
    expect(query.get("sort")).toBe("createdAt");
    expect(query.get("order")).toBe("desc");
  });

  /** Request only backend PNG CAPTCHA and preserve a body-free anonymous contract call. */
  it("requests a body-free backend CAPTCHA with browser credentials", async () => {
    let capturedRequest: HttpTransportRequest | undefined;
    setHttpTransportForTests(async (request) => {
      capturedRequest = request;
      return jsonResponse(201, {
        challengeId: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
        imageDataUrl: "data:image/png;base64,ZmFrZQ==",
        expiresAt: "2026-07-26T00:02:00.000Z",
      });
    });

    const captcha = await createLoginCaptcha();

    expect(captcha.imageDataUrl.startsWith("data:image/png;base64,")).toBe(true);
    expect(capturedRequest?.init.method).toBe("POST");
    expect(capturedRequest?.init.body).toBeUndefined();
    expect(capturedRequest?.init.credentials).toBe("include");
  });
});
