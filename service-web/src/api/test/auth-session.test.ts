import { afterEach, beforeEach, describe, expect, it } from "vite-plus/test";

import { createLoginCaptcha } from "../auth";
import { authMeQueryKey, authSession } from "../auth-session";
import { queryClient } from "../query-client";
import { ApiError, setHttpTransportForTests } from "../http";
import { listUsers } from "../users";
import type { HttpTransportRequest } from "../http";

/** 为受控传输测试构造契约形状的 JSON 响应。 */
function jsonResponse(status: number, body: unknown, headers: HeadersInit = {}): Response {
  const responseHeaders = new Headers(headers);
  responseHeaders.set("Content-Type", "application/json");

  return new Response(JSON.stringify(body), {
    status,
    headers: responseHeaders,
  });
}

/** 返回不含凭据、token 或验证码秘密的当前用户载荷。 */
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
  /** 在隔离测试之间重置全局进程内鉴权与传输状态。 */
  beforeEach(() => {
    authSession.clear();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  /** 每个受控测试适配器结束后恢复生产传输。 */
  afterEach(() => {
    setHttpTransportForTests();
    authSession.clear();
    window.history.replaceState(null, "", "/");
  });

  /** access token 仅保存在内存，身份写入鉴权查询缓存。 */
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
    expect(requests.every((request) => request.init.method === "POST")).toBe(true);
    expect(new Headers(requests[1]?.init.headers).get("Authorization")).toBe(
      "Bearer short-lived-token",
    );
  });

  /** 并发会话恢复加入同一请求，避免 refresh cookie 被旋转两次。 */
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

  /** 仅将 refresh 401 视为终态会话失败，并清除受保护客户端状态。 */
  it("clears identity only when refresh rejects credentials with 401", async () => {
    setHttpTransportForTests(async () => jsonResponse(401, { code: "invalid-refresh-token" }));

    await expect(authSession.ensureSession()).resolves.toBeNull();

    expect(authSession.getAccessToken()).toBeUndefined();
    expect(queryClient.getQueryData(authMeQueryKey)).toBeUndefined();
    expect(authSession.getSnapshot().status).toBe("anonymous");
  });

  /** 清理会话时移除 ETF typed market-data 缓存，避免受保护数据跨账号残留。 */
  it("clears protected ETF market-data queries with the session", () => {
    queryClient.setQueryData(["market-data", "etf", "profiles"], { records: ["protected"] });
    queryClient.setQueryData(["public-reference"], { value: "retained" });

    authSession.clear();

    expect(queryClient.getQueryData(["market-data", "etf", "profiles"])).toBeUndefined();
    expect(queryClient.getQueryData(["public-reference"])).toEqual({ value: "retained" });
  });

  /** 上抛 refresh 403，同时保留非敏感身份与用户缓存状态。 */
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

  /** 受保护操作返回 403 时保留已认证身份，不误判为匿名。 */
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

  /** access token 过期后刷新并重放原请求，期间保持 URL 与已认证 UI 不变。 */
  it("refreshes and retries once after a protected API 401 response", async () => {
    let refreshCalls = 0;
    let listCalls = 0;
    // 模拟列表首次 401、刷新成功及携带新 token 的 POST 重放。
    setHttpTransportForTests(async (request) => {
      if (request.url.endsWith("/api/v1/auth/login")) {
        return jsonResponse(200, {
          accessToken: "short-lived-token",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (request.url.endsWith("/api/v1/auth/refresh")) {
        refreshCalls += 1;
        return jsonResponse(200, {
          accessToken: "refreshed-token",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (request.url.includes("/api/v1/users/list?")) {
        listCalls += 1;
        const authorization = new Headers(request.init.headers).get("Authorization");
        return authorization === "Bearer refreshed-token"
          ? jsonResponse(200, { items: [], page: { nextCursor: null } })
          : jsonResponse(401, { code: "access-token-expired" });
      }

      return jsonResponse(200, currentUserPayload());
    });
    await authSession.login({
      account: "admin.demo",
      password: "secure-pass-123",
      captchaId: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
      captchaAnswer: "1234",
    });

    await expect(listUsers({ sort: "createdAt", order: "desc", pageSize: 20 })).resolves.toEqual({
      items: [],
      page: { nextCursor: null },
    });

    expect(refreshCalls).toBe(1);
    expect(listCalls).toBe(2);
    expect(authSession.getAccessToken()).toBe("refreshed-token");
    expect(queryClient.getQueryData(authMeQueryKey)).toEqual(currentUserPayload());
    expect(authSession.getSnapshot().status).toBe("authenticated");
  });

  /** 后台校验新 token 时保留旧身份与当前 URL，避免壳层闪回加载页。 */
  it("retains authenticated UI state while a refreshed token is verified", async () => {
    let currentUserCalls = 0;
    let markVerificationStarted!: () => void;
    let releaseVerification!: () => void;
    /** 让测试等待第二次当前用户校验真正开始。 */
    const verificationStarted = new Promise<void>((resolve) => {
      markVerificationStarted = resolve;
    });
    /** 将第二次当前用户校验暂停在服务端响应前。 */
    const verificationGate = new Promise<void>((resolve) => {
      releaseVerification = resolve;
    });

    // 模拟刷新后当前用户校验延迟，保留既有认证界面。
    setHttpTransportForTests(async (request) => {
      if (request.url.endsWith("/api/v1/auth/login")) {
        return jsonResponse(200, {
          accessToken: "short-lived-token",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (request.url.endsWith("/api/v1/auth/refresh")) {
        return jsonResponse(200, {
          accessToken: "refreshed-token",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (request.url.includes("/api/v1/users/list?")) {
        const authorization = new Headers(request.init.headers).get("Authorization");
        return authorization === "Bearer refreshed-token"
          ? jsonResponse(200, { items: [], page: { nextCursor: null } })
          : jsonResponse(401, { code: "access-token-expired" });
      }
      if (request.url.endsWith("/api/v1/users/me")) {
        currentUserCalls += 1;
        if (currentUserCalls === 2) {
          markVerificationStarted();
          await verificationGate;
        }
      }

      return jsonResponse(200, currentUserPayload());
    });
    await authSession.login({
      account: "admin.demo",
      password: "secure-pass-123",
      captchaId: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
      captchaAnswer: "1234",
    });
    window.history.replaceState(null, "", "/users?sort=createdAt");
    const expectedUrl = window.location.href;

    const pendingRequest = listUsers({ sort: "createdAt", order: "desc", pageSize: 20 });
    await verificationStarted;

    expect(authSession.getSnapshot().status).toBe("authenticated");
    expect(queryClient.getQueryData(authMeQueryKey)).toEqual(currentUserPayload());
    expect(window.location.href).toBe(expectedUrl);

    releaseVerification();
    await expect(pendingRequest).resolves.toEqual({
      items: [],
      page: { nextCursor: null },
    });
    expect(window.location.href).toBe(expectedUrl);
  });

  /** 多个过期请求共享一次 refresh，各自用新 token 重放一次。 */
  it("uses one refresh for concurrent protected API 401 responses", async () => {
    let refreshCalls = 0;
    // 模拟多个 POST 列表请求共享一次 refresh。
    setHttpTransportForTests(async (request) => {
      if (request.url.endsWith("/api/v1/auth/login")) {
        return jsonResponse(200, {
          accessToken: "short-lived-token",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (request.url.endsWith("/api/v1/auth/refresh")) {
        refreshCalls += 1;
        return jsonResponse(200, {
          accessToken: "refreshed-token",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (request.url.includes("/api/v1/users/list?")) {
        const authorization = new Headers(request.init.headers).get("Authorization");
        return authorization === "Bearer refreshed-token"
          ? jsonResponse(200, { items: [], page: { nextCursor: null } })
          : jsonResponse(401, { code: "access-token-expired" });
      }

      return jsonResponse(200, currentUserPayload());
    });
    await authSession.login({
      account: "admin.demo",
      password: "secure-pass-123",
      captchaId: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
      captchaAnswer: "1234",
    });

    await Promise.all([
      listUsers({ sort: "createdAt", order: "desc", pageSize: 20 }),
      listUsers({ sort: "createdAt", order: "desc", pageSize: 20 }),
    ]);

    expect(refreshCalls).toBe(1);
    expect(authSession.getAccessToken()).toBe("refreshed-token");
  });

  /** refresh cookie 被拒绝时才清理身份，并通知壳层站内跳转。 */
  it("clears identity when refresh after a protected 401 is rejected", async () => {
    // 模拟列表过期后 refresh cookie 被拒绝。
    setHttpTransportForTests(async (request) => {
      if (request.url.endsWith("/api/v1/auth/login")) {
        return jsonResponse(200, {
          accessToken: "short-lived-token",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (request.url.endsWith("/api/v1/auth/refresh")) {
        return jsonResponse(401, { code: "invalid-refresh-token" });
      }
      if (request.url.includes("/api/v1/users/list?")) {
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

  /** 每个列表请求都包含稳定的默认创建时间降序。 */
  it("sends the default created-time descending list order", async () => {
    let userListRequest: HttpTransportRequest | undefined;
    // 捕获 POST 用户列表请求以核对 URL 查询参数。
    setHttpTransportForTests(async (request) => {
      if (request.url.endsWith("/api/v1/auth/login")) {
        return jsonResponse(200, {
          accessToken: "short-lived-token",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (request.url.includes("/api/v1/users/list?")) {
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

  /** 仅请求后端 PNG 验证码，并保持匿名契约调用无请求体。 */
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
