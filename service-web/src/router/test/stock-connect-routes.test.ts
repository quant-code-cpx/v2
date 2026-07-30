import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import type { LoaderFunctionArgs } from "react-router-dom";

import { authSession } from "../../api/auth-session";
import { createNavigationGroups } from "../../components/AppShell/navigation";
import type { CurrentUser } from "../../types/access";
import { requireStockConnectChannel, requireStockConnectSecurity } from "../stock-connect-loaders";

/** 返回路由守卫使用的已认证普通用户。 */
function currentUser(): CurrentUser {
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

/** 构造指定 params 的 React Router loader 上下文。 */
function loaderArguments(params: Record<string, string>): LoaderFunctionArgs {
  return {
    request: new Request("http://quant.local/market/stock-connect"),
    url: new URL("http://quant.local/market/stock-connect"),
    pattern: "/market/stock-connect",
    params,
    context: undefined,
  };
}

/** 验证互联互通路由边界、稳定短名和侧栏深层激活。 */
describe("stock connect routes", () => {
  /** 每个路由测试恢复真实会话协调器。 */
  afterEach(() => {
    vi.restoreAllMocks();
  });

  /** 四个冻结短名允许访问，未知短名在发送 API 前返回站内 404。 */
  it("accepts only the four channel slugs", async () => {
    vi.spyOn(authSession, "ensureSession").mockResolvedValue(currentUser());

    await expect(
      requireStockConnectChannel(loaderArguments({ channel: "sh-southbound" })),
    ).resolves.toMatchObject({ role: "USER" });
    await expect(
      requireStockConnectChannel(loaderArguments({ channel: "global" })),
    ).rejects.toMatchObject({ status: 404 });
  });

  /** 证券引用空值或超长值必须在路由边界拒绝。 */
  it("bounds instrument entity references before querying", async () => {
    vi.spyOn(authSession, "ensureSession").mockResolvedValue(currentUser());

    await expect(
      requireStockConnectSecurity(loaderArguments({ instrumentEntityRef: "instrument:hkex:001" })),
    ).resolves.toMatchObject({ role: "USER" });
    await expect(
      requireStockConnectSecurity(loaderArguments({ instrumentEntityRef: "x".repeat(161) })),
    ).rejects.toMatchObject({ status: 404 });
  });

  /** 侧栏入口必须覆盖总览、通道详情和证券上下文子路由。 */
  it("declares stock connect navigation with explicit active prefix", () => {
    const item = createNavigationGroups()
      .flatMap(
        /** 展平稳定业务导航组。 */
        (group) => group.items,
      )
      .find(
        /** 查找唯一互联互通入口。 */
        (candidate) => candidate.to === "/market/stock-connect",
      );

    expect(item).toMatchObject({
      label: "互联互通",
      activePrefixes: ["/market/stock-connect"],
    });
  });
});
