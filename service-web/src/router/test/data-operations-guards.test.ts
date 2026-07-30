import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import type { LoaderFunctionArgs } from "react-router-dom";

import { authSession } from "../../api/auth-session";
import { createNavigationGroups } from "../../components/AppShell/navigation";
import { requireDataOperationsAccess } from "../guards";
import type { CurrentUser, UserRole } from "../../types/access";

/** 构造路由守卫需要的最小合法受保护请求上下文。 */
function loaderArguments(): LoaderFunctionArgs {
  return {
    request: new Request("http://apex.local/data-operations"),
    url: new URL("http://apex.local/data-operations"),
    pattern: "/data-operations",
    params: {},
    context: undefined,
  };
}

/** 构造服务端已认证的角色投影，权限列表不参与数据运维角色判定。 */
function currentUser(role: UserRole): CurrentUser {
  return {
    id: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
    account: `${role.toLowerCase()}.demo`,
    displayName: role,
    role,
    status: "ACTIVE",
    version: 1,
    lastLoginAt: null,
    deletedAt: null,
    createdAt: "2026-07-29T00:00:00.000Z",
    updatedAt: "2026-07-29T00:00:00.000Z",
    permissions: [],
  };
}

describe("data operations route access", () => {
  /** 每个测试恢复真实会话协调器方法，避免污染其他鉴权测试。 */
  afterEach(() => {
    vi.restoreAllMocks();
  });

  /** ADMIN 和 SUPER_ADMIN 都可读取，普通 USER 在路由层直接被拒绝。 */
  it("accepts ADMIN and SUPER_ADMIN but rejects USER", async () => {
    const ensureSession = vi.spyOn(authSession, "ensureSession");

    ensureSession.mockResolvedValueOnce(currentUser("ADMIN"));
    await expect(requireDataOperationsAccess(loaderArguments())).resolves.toMatchObject({
      role: "ADMIN",
    });

    ensureSession.mockResolvedValueOnce(currentUser("SUPER_ADMIN"));
    await expect(requireDataOperationsAccess(loaderArguments())).resolves.toMatchObject({
      role: "SUPER_ADMIN",
    });

    ensureSession.mockResolvedValueOnce(currentUser("USER"));
    await expect(requireDataOperationsAccess(loaderArguments())).rejects.toMatchObject({
      status: 403,
    });
  });

  /** 侧栏只为两个管理角色暴露数据运维入口。 */
  it("declares data operations navigation for read-capable roles only", () => {
    const item = createNavigationGroups()
      .flatMap((group) => group.items)
      .find((candidate) => candidate.to === "/data-operations");

    expect(item?.allowedRoles).toEqual(["ADMIN", "SUPER_ADMIN"]);
  });
});
