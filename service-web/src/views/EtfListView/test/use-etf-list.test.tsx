import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vite-plus/test";

import { authSession } from "../../../api/auth-session";
import { setHttpTransportForTests } from "../../../api/http";
import type { HttpTransportRequest } from "../../../api/http";
import { useEtfList } from "../hooks/useEtfList";

/** 返回建立测试会话所需的最小当前用户。 */
function currentUserPayload() {
  return {
    id: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
    account: "market.user",
    displayName: "市场用户",
    role: "USER",
    status: "ACTIVE",
    version: 1,
    lastLoginAt: null,
    deletedAt: null,
    createdAt: "2026-07-30T00:00:00.000Z",
    updatedAt: "2026-07-30T00:00:00.000Z",
    permissions: [],
  };
}

/** 构造共享传输层可识别的 JSON 响应。 */
function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** 为 ETF 列表 Hook 提供隔离的 QueryClient 和带失效 cursor 的初始 URL。 */
function TestProviders({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/market/etfs?q=沪深&page=2&cursor=expired-cursor"]}>
        {children}
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("useEtfList", () => {
  /** 每个用例后恢复真实浏览器传输，避免影响其他 API 测试。 */
  afterEach(() => {
    setHttpTransportForTests();
    authSession.clear();
  });

  it("publication 更新使 cursor 返回 409 时自动回到同筛选首页", async () => {
    setHttpTransportForTests(
      /** 建立真实内存会话后，仅让 market-data 拒绝旧 publication 的 cursor。 */
      async (request: HttpTransportRequest) => {
        const path = new URL(request.url, "http://apex.local").pathname;
        if (path === "/api/v1/auth/login") {
          return jsonResponse(200, {
            accessToken: "access-market",
            accessTokenExpiresIn: 600,
            user: currentUserPayload(),
          });
        }
        if (path === "/api/v1/users/me") {
          return jsonResponse(200, currentUserPayload());
        }

        return jsonResponse(409, { code: "MARKET_DATA_CURSOR_CONFLICT" });
      },
    );
    await authSession.login({
      account: "market.user",
      password: "secure-pass-123",
      captchaId: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
      captchaAnswer: "1234",
    });

    const { result } = renderHook(
      /** 读取 Hook 状态，等待失效 cursor 的恢复副作用完成。 */
      () => useEtfList(),
      { wrapper: TestProviders },
    );

    expect(result.current.filters.page).toBe(2);
    expect(result.current.filters.cursor).toBe("expired-cursor");
    expect(result.current.cursorRecoveryNotice).toBe(false);

    await waitFor(() => {
      expect(result.current.filters.page).toBe(1);
      expect(result.current.filters.cursor).toBeUndefined();
      expect(result.current.filters.q).toBe("沪深");
      expect(result.current.cursorRecoveryNotice).toBe(true);
    });
    act(() => {
      result.current.dismissCursorRecoveryNotice();
    });
    expect(result.current.cursorRecoveryNotice).toBe(false);
  });

  /** 伪造或截断 cursor 返回公开 400 validation-error 时只回首页一次，不重复发送坏 cursor。 */
  it("resets a rejected validation cursor once without retrying the bad value", async () => {
    let badCursorRequests = 0;
    setHttpTransportForTests(
      /** 仅当请求继续携带坏 cursor 时计数，首页请求返回独立依赖错误。 */
      async (request: HttpTransportRequest) => {
        const path = new URL(request.url, "http://apex.local").pathname;
        if (path === "/api/v1/auth/login") {
          return jsonResponse(200, {
            accessToken: "access-market",
            accessTokenExpiresIn: 600,
            user: currentUserPayload(),
          });
        }
        if (path === "/api/v1/users/me") {
          return jsonResponse(200, currentUserPayload());
        }
        const body =
          typeof request.init.body === "string"
            ? (JSON.parse(request.init.body) as { page?: { cursor?: string } })
            : {};
        if (body.page?.cursor !== undefined) {
          badCursorRequests += 1;
          return jsonResponse(400, { code: "validation-error" });
        }

        return jsonResponse(503, { code: "dependency-unavailable" });
      },
    );
    await authSession.login({
      account: "market.user",
      password: "secure-pass-123",
      captchaId: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
      captchaAnswer: "1234",
    });

    const { result } = renderHook(
      /** 读取 Hook 状态，等待一次性清除非法 cursor。 */
      () => useEtfList(),
      { wrapper: TestProviders },
    );

    await waitFor(() => {
      expect(result.current.filters.page).toBe(1);
      expect(result.current.filters.cursor).toBeUndefined();
      expect(result.current.filters.q).toBe("沪深");
      expect(result.current.cursorRecoveryNotice).toBe(true);
    });
    expect(badCursorRequests).toBe(1);
  });
});
