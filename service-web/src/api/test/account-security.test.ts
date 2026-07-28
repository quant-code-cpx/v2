import { afterEach, beforeEach, describe, expect, it } from "vite-plus/test";

import {
  listSessionFamilies,
  revokeOtherSessionFamilies,
  revokeSessionFamily,
} from "../account-security";
import { changeCurrentPassword } from "../account";
import { getAuditEvent, listAuditEvents } from "../audit-events";
import { authSession } from "../auth-session";
import { setHttpTransportForTests } from "../http";
import { getManageableUserStatistics } from "../users";
import type { HttpTransportRequest } from "../http";

/** 固定合同测试使用的 UUID。 */
const identifiers = {
  user: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
  family: "7ce0f18a-9f4d-4b3a-ae69-d0ff1707df91",
  event: "72a4d2a1-3798-4bcf-978f-75c69c6d246b",
} as const;

/** 构造不含敏感材料的当前用户响应。 */
function currentUserPayload() {
  return {
    id: identifiers.user,
    account: "super.demo",
    displayName: "超级管理员",
    role: "SUPER_ADMIN",
    status: "ACTIVE",
    version: 1,
    lastLoginAt: "2026-07-28T06:00:00.000Z",
    deletedAt: null,
    createdAt: "2026-07-20T00:00:00.000Z",
    updatedAt: "2026-07-28T06:00:00.000Z",
    permissions: ["sessions:read", "sessions:revoke", "users:read", "audit:read"],
  };
}

/** 构造带 JSON Content-Type 的受控响应。 */
function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** 读取共享传输层生成的字符串 JSON 请求体。 */
function parseRequestBody(request: HttpTransportRequest | undefined): unknown {
  const body = request?.init.body;

  if (typeof body !== "string") {
    throw new TypeError("Expected a string JSON request body.");
  }

  return JSON.parse(body) as unknown;
}

/** 登录受控会话，使合同 0017 客户端经真实鉴权协调器发请求。 */
async function establishSession(): Promise<void> {
  await authSession.login({
    account: "super.demo",
    password: "secure-pass-123",
    captchaId: identifiers.user,
    captchaAnswer: "1234",
  });
}

describe("account security API", () => {
  /** 每个测试前清空查询与内存 token。 */
  beforeEach(() => {
    authSession.clear();
  });

  /** 每个测试后恢复浏览器传输并移除安全缓存。 */
  afterEach(() => {
    setHttpTransportForTests();
    authSession.clear();
  });

  /** 六个新客户端只能经共享传输层发送 POST，并遵循冻结请求体。 */
  it("uses POST-only transport and parses accepted contract payloads", async () => {
    const requests: HttpTransportRequest[] = [];
    setHttpTransportForTests(async (request) => {
      requests.push(request);
      const path = new URL(request.url, "http://apex.local").pathname;

      if (path === "/api/v1/auth/login") {
        return jsonResponse(200, {
          accessToken: "access-super",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") {
        return jsonResponse(200, currentUserPayload());
      }
      if (path === "/api/v1/auth/sessions/list") {
        return jsonResponse(200, {
          items: [
            {
              familyId: identifiers.family,
              current: true,
              lastActiveAt: "2026-07-28T06:00:00.000Z",
              absoluteExpiresAt: "2026-08-04T06:00:00.000Z",
            },
          ],
          page: { nextCursor: null },
          total: 1,
        });
      }
      if (path.endsWith("/revoke")) {
        return new Response(null, { status: 204 });
      }
      if (path === "/api/v1/auth/sessions/revoke-others") {
        return jsonResponse(200, { revokedFamilyCount: 0 });
      }
      if (path === "/api/v1/audit-events/list") {
        return jsonResponse(200, {
          items: [],
          page: { nextCursor: null },
          appliedWindow: {
            occurredFrom: "2026-07-21T06:00:00.000Z",
            occurredTo: "2026-07-28T06:00:00.000Z",
          },
        });
      }
      if (path === `/api/v1/audit-events/${identifiers.event}`) {
        return jsonResponse(200, auditDetailPayload());
      }
      if (path === "/api/v1/users/statistics") {
        return jsonResponse(200, {
          generatedAt: "2026-07-28T06:00:00.000Z",
          scope: ["USER", "ADMIN"],
          total: 3,
          active: 2,
          disabled: 1,
          deleted: 0,
          loggedInLast30Days: 2,
          byRole: [
            { role: "USER", total: 2, active: 2, disabled: 0, deleted: 0 },
            { role: "ADMIN", total: 1, active: 0, disabled: 1, deleted: 0 },
          ],
        });
      }

      return jsonResponse(404, {});
    });
    await establishSession();

    await expect(listSessionFamilies({ pageSize: 20 })).resolves.toMatchObject({ total: 1 });
    await expect(revokeSessionFamily(identifiers.family)).resolves.toBeUndefined();
    await expect(revokeOtherSessionFamilies()).resolves.toEqual({ revokedFamilyCount: 0 });
    await expect(
      listAuditEvents({
        occurredFrom: "2026-07-21T06:00:00.000Z",
        occurredTo: "2026-07-28T06:00:00.000Z",
        includeRoutine: false,
        pageSize: 20,
      }),
    ).resolves.toMatchObject({ items: [] });
    await expect(getAuditEvent(identifiers.event)).resolves.toMatchObject({
      id: identifiers.event,
    });
    await expect(getManageableUserStatistics()).resolves.toMatchObject({ total: 3 });

    expect(requests.every((request) => request.init.method === "POST")).toBe(true);
    const sessionListRequest = requests.find((request) =>
      request.url.endsWith("/api/v1/auth/sessions/list"),
    );
    expect(parseRequestBody(sessionListRequest)).toEqual({ pageSize: 20 });
    const auditListRequest = requests.find((request) =>
      request.url.endsWith("/api/v1/audit-events/list"),
    );
    expect(parseRequestBody(auditListRequest)).toMatchObject({
      includeRoutine: false,
      pageSize: 20,
    });
  });

  /** Zod 严格拒绝服务端把原始 metadata 混入审计详情。 */
  it("rejects audit details containing raw metadata", async () => {
    setHttpTransportForTests(async (request) => {
      const path = new URL(request.url, "http://apex.local").pathname;

      if (path === "/api/v1/auth/login") {
        return jsonResponse(200, {
          accessToken: "access-super",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") {
        return jsonResponse(200, currentUserPayload());
      }

      return jsonResponse(200, { ...auditDetailPayload(), metadata: { token: "forbidden" } });
    });
    await establishSession();

    await expect(getAuditEvent(identifiers.event)).rejects.toMatchObject({
      name: "ZodError",
    });
  });

  /** 当前密码错误不触发 token refresh，也不误清已认证身份。 */
  it("keeps the session after a current-password-invalid response", async () => {
    let refreshCalls = 0;
    setHttpTransportForTests(async (request) => {
      const path = new URL(request.url, "http://apex.local").pathname;

      if (path === "/api/v1/auth/login") {
        return jsonResponse(200, {
          accessToken: "access-super",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") {
        return jsonResponse(200, currentUserPayload());
      }
      if (path === "/api/v1/auth/refresh") {
        refreshCalls += 1;
        return jsonResponse(200, {
          accessToken: "refreshed-super",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }

      return jsonResponse(401, {
        code: "current-password-invalid",
      });
    });
    await establishSession();

    await expect(
      changeCurrentPassword({
        currentPassword: "incorrect",
        newPassword: "secure-pass-456",
      }),
    ).rejects.toMatchObject({ status: 401, code: "current-password-invalid" });

    expect(refreshCalls).toBe(0);
    expect(authSession.getSnapshot().status).toBe("authenticated");
    expect(authSession.getAccessToken()).toBe("access-super");
  });
});

/** 构造只含合同 allowlist 的审计详情。 */
function auditDetailPayload() {
  return {
    id: identifiers.event,
    category: "AUTHENTICATION",
    severity: "WARNING",
    action: "auth.refresh.replay_detected",
    summary: "检测到 Refresh 重放",
    actor: null,
    target: { type: "SESSION", id: identifiers.family },
    requestId: "e2e-request-id",
    occurredAt: "2026-07-28T06:00:00.000Z",
    details: { revokedFamilyCount: 1 },
  };
}
