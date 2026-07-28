import { expect, test } from "@playwright/test";
import type { Locator, Page, Route } from "@playwright/test";

/** 使用固定有效 UUID，确保 URL 所有的 Dialog 状态通过目标合同校验。 */
const ids = {
  user: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
  admin: "7ce0f18a-9f4d-4b3a-ae69-d0ff1707df91",
  super: "72a4d2a1-3798-4bcf-978f-75c69c6d246b",
  created: "8f401b48-5b0e-4a76-8d85-2c7101a28955",
  deleted: "9eb2c698-6401-4bd5-81b2-f3a7900ea87b",
  family: "47bd3b4f-316a-4cbb-a35c-1785887e9013",
  event: "5f1aa8af-f515-4f6b-af1c-b7f00ee436db",
} as const;

/** 表示受控 API 路由处理器接受的测试角色场景。 */
type Scenario = "user" | "admin" | "super";

/** 表示严格合同测试响应使用的非敏感用户形状。 */
interface MockUser {
  id: string;
  account: string;
  displayName: string;
  role: "USER" | "ADMIN" | "SUPER_ADMIN";
  status: "ACTIVE" | "DISABLED" | "DELETED";
  version: number;
  lastLoginAt: string | null;
  deletedAt: string | null;
  createdAt: string;
  updatedAt: string;
}

/** 跟踪测试后端行为，使权限测试能证明受限列表未发起。 */
interface MockApiState {
  users: MockUser[];
  userListCalls: number;
  unauthorizedUserListCalls: number;
  forceRefreshUnauthorized: boolean;
  forceUserListUnauthorized: boolean;
  forceUserListError: boolean;
  forceProfileConflict: boolean;
  auditListCalls: number;
}

/** 返回确定性 E2E 响应 fixture 共享的合同时间。 */
function timestamps() {
  return {
    lastLoginAt: "2026-07-26T09:20:00.000Z",
    deletedAt: null,
    createdAt: "2026-07-26T08:00:00.000Z",
    updatedAt: "2026-07-26T09:20:00.000Z",
  };
}

/** 构造符合场景服务端权限计算结果的当前身份。 */
function currentUser(scenario: Scenario) {
  const base = {
    id: scenario === "user" ? ids.user : scenario === "admin" ? ids.admin : ids.super,
    account: `${scenario}.demo`,
    displayName: scenario === "user" ? "普通用户" : scenario === "admin" ? "管理员" : "超级管理员",
    role: scenario === "user" ? "USER" : scenario === "admin" ? "ADMIN" : "SUPER_ADMIN",
    status: "ACTIVE",
    version: 1,
    ...timestamps(),
  } as const;

  return {
    ...base,
    permissions:
      scenario === "user"
        ? ["profile:read", "profile:update", "password:change", "sessions:read", "sessions:revoke"]
        : scenario === "admin"
          ? [
              "profile:read",
              "profile:update",
              "password:change",
              "sessions:read",
              "sessions:revoke",
              "users:read",
              "users:create",
              "users:update",
              "users:delete",
              "users:reset-password",
            ]
          : [
              "profile:read",
              "profile:update",
              "password:change",
              "sessions:read",
              "sessions:revoke",
              "users:read",
              "users:create",
              "users:update",
              "users:delete",
              "users:reset-password",
              "admins:create",
              "admins:manage",
              "audit:read",
            ],
  };
}

/** 只从内存 Web 客户端提供的测试 access token 读取场景。 */
function scenarioFromRoute(route: Route): Scenario {
  const authorization = route.request().headers().authorization ?? "";

  if (authorization.includes("super")) {
    return "super";
  }
  if (authorization.includes("user")) {
    return "user";
  }

  return "admin";
}

/** 页面刷新后从测试专用 HttpOnly cookie 替身恢复 E2E 身份。 */
function scenarioFromRefreshCookie(route: Route): Scenario | undefined {
  const cookie = route.request().headers().cookie ?? "";

  if (cookie.includes("e2e_session=super.demo")) {
    return "super";
  }
  if (cookie.includes("e2e_session=user.demo")) {
    return "user";
  }
  if (cookie.includes("e2e_session=admin.demo")) {
    return "admin";
  }

  return undefined;
}

/** 用兼容 no-store 的测试 Body 返回 JSON API 响应。 */
async function fulfilJson(
  route: Route,
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
) {
  await route.fulfill({
    status,
    contentType: "application/json",
    headers,
    body: JSON.stringify(body),
  });
}

/** 返回不包含敏感 detail 的稳定 Problem 响应。 */
async function fulfilProblem(route: Route, status: number, code: string) {
  await fulfilJson(route, status, {
    type: `https://apex.local/problems/${code}`,
    title: status === 403 ? "Forbidden" : status === 401 ? "Unauthorized" : "Service Unavailable",
    status,
    detail: "Request rejected",
    instance: new URL(route.request().url()).pathname,
    requestId: "e2e-request-id",
    code,
  });
}

/** 解析仅供受控 E2E API 适配器使用的 JSON 请求体。 */
function requestJson(route: Route): Record<string, unknown> {
  const rawBody = route.request().postData() ?? "{}";
  const parsedBody: unknown = JSON.parse(rawBody);

  return typeof parsedBody === "object" && parsedBody !== null
    ? (parsedBody as Record<string, unknown>)
    : {};
}

/** 构造合同 0017 允许公开的脱敏审计事件。 */
function auditEvent() {
  return {
    id: ids.event,
    category: "AUTHENTICATION",
    severity: "WARNING",
    action: "auth.refresh.replay_detected",
    summary: "检测到 Refresh 重放",
    actor: null,
    target: { type: "SESSION", id: ids.family },
    requestId: "91e2-e2e-8a40",
    occurredAt: "2026-07-28T06:21:08.000Z",
  };
}

/** 为单个浏览器页面安装严格的合同 0002 API 适配器；生产代码仍使用 fetch。 */
async function installMockApi(page: Page): Promise<MockApiState> {
  const state: MockApiState = {
    users: [
      {
        id: ids.user,
        account: "market.user",
        displayName: "市场用户",
        role: "USER",
        status: "ACTIVE",
        version: 1,
        ...timestamps(),
      },
      {
        id: ids.admin,
        account: "research.admin",
        displayName: "研究管理员",
        role: "ADMIN",
        status: "DISABLED",
        version: 1,
        ...timestamps(),
      },
      {
        id: ids.deleted,
        account: "archived.user",
        displayName: "已删除用户",
        role: "USER",
        status: "DELETED",
        version: 1,
        ...timestamps(),
        deletedAt: "2026-07-26T09:30:00.000Z",
      },
    ],
    userListCalls: 0,
    unauthorizedUserListCalls: 0,
    forceRefreshUnauthorized: false,
    forceUserListUnauthorized: false,
    forceUserListError: false,
    forceProfileConflict: false,
    auditListCalls: 0,
  };

  /** 仅拦截版本化 API 调用，并为 UI 验收测试返回合同形状。 */
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    expect(request.method()).toBe("POST");
    const url = new URL(request.url());
    const path = url.pathname;
    const scenario = scenarioFromRoute(route);

    if (path === "/api/v1/auth/captcha" && request.method() === "POST") {
      await fulfilJson(route, 201, {
        challengeId: ids.user,
        imageDataUrl:
          "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9J9F0AAAAASUVORK5CYII=",
        expiresAt: "2026-07-26T10:02:00.000Z",
      });
      return;
    }
    if (path === "/api/v1/auth/refresh" && request.method() === "POST") {
      if (state.forceRefreshUnauthorized) {
        await fulfilProblem(route, 401, "invalid-refresh-token");
        return;
      }
      const refreshScenario = scenarioFromRefreshCookie(route);
      if (refreshScenario !== undefined) {
        await fulfilJson(route, 200, {
          accessToken: `access-${refreshScenario}`,
          accessTokenExpiresIn: 600,
          user: currentUser(refreshScenario),
        });
        return;
      }
      await fulfilProblem(route, 401, "invalid-refresh-token");
      return;
    }
    if (path === "/api/v1/auth/login" && request.method() === "POST") {
      const input = requestJson(route);
      const account = typeof input.account === "string" ? input.account : "admin.demo";
      const loginScenario: Scenario = account.startsWith("user")
        ? "user"
        : account.startsWith("super")
          ? "super"
          : "admin";
      await fulfilJson(route, 200, {
        accessToken: `access-${loginScenario}`,
        accessTokenExpiresIn: 600,
        user: currentUser(loginScenario),
      });
      return;
    }
    if (path === "/api/v1/auth/logout" && request.method() === "POST") {
      await route.fulfill({
        status: 204,
        headers: {
          "Set-Cookie": "e2e_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax",
        },
      });
      return;
    }
    if (path === "/api/v1/users/me" && request.method() === "POST") {
      await fulfilJson(route, 200, currentUser(scenario), {
        ETag: `"${currentUser(scenario).id}-v1"`,
      });
      return;
    }
    if (path === "/api/v1/users/me/update" && request.method() === "POST") {
      if (state.forceProfileConflict) {
        state.forceProfileConflict = false;
        await fulfilProblem(route, 412, "precondition-failed");
        return;
      }
      const input = requestJson(route);
      const updated = {
        ...currentUser(scenario),
        displayName:
          typeof input.displayName === "string"
            ? input.displayName
            : currentUser(scenario).displayName,
        version: 2,
      };
      await fulfilJson(route, 200, updated, { ETag: `"${updated.id}-v2"` });
      return;
    }
    if (path === "/api/v1/users/me/password" && request.method() === "POST") {
      await route.fulfill({
        status: 204,
        headers: {
          "Set-Cookie": "e2e_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax",
        },
      });
      return;
    }
    if (path === "/api/v1/auth/sessions/list" && request.method() === "POST") {
      await fulfilJson(route, 200, {
        items: [
          {
            familyId: ids.family,
            current: true,
            lastActiveAt: "2026-07-28T06:28:00.000Z",
            absoluteExpiresAt: "2026-08-04T06:28:00.000Z",
          },
          {
            familyId: ids.admin,
            current: false,
            lastActiveAt: "2026-07-28T01:16:00.000Z",
            absoluteExpiresAt: "2026-08-03T01:16:00.000Z",
          },
        ],
        page: { nextCursor: null },
        total: 2,
      });
      return;
    }
    if (path === "/api/v1/auth/sessions/revoke-others" && request.method() === "POST") {
      await fulfilJson(route, 200, { revokedFamilyCount: 1 });
      return;
    }
    if (/^\/api\/v1\/auth\/sessions\/[0-9a-f-]+\/revoke$/iu.test(path)) {
      await route.fulfill({ status: 204 });
      return;
    }
    if (path === "/api/v1/users/statistics" && request.method() === "POST") {
      await fulfilJson(route, 200, {
        generatedAt: "2026-07-28T06:32:00.000Z",
        scope: scenario === "super" ? ["USER", "ADMIN"] : ["USER"],
        total: scenario === "super" ? 126 : 112,
        active: scenario === "super" ? 118 : 108,
        disabled: scenario === "super" ? 6 : 3,
        deleted: scenario === "super" ? 2 : 1,
        loggedInLast30Days: 92,
        byRole:
          scenario === "super"
            ? [
                { role: "USER", total: 112, active: 108, disabled: 3, deleted: 1 },
                { role: "ADMIN", total: 14, active: 10, disabled: 3, deleted: 1 },
              ]
            : [{ role: "USER", total: 112, active: 108, disabled: 3, deleted: 1 }],
      });
      return;
    }
    if (path === "/api/v1/audit-events/list" && request.method() === "POST") {
      state.auditListCalls += 1;
      if (scenario !== "super") {
        await fulfilProblem(route, 403, "forbidden");
        return;
      }
      await fulfilJson(route, 200, {
        items: [auditEvent()],
        page: { nextCursor: null },
        appliedWindow: {
          occurredFrom: "2026-07-21T06:32:00.000Z",
          occurredTo: "2026-07-28T06:32:00.000Z",
        },
      });
      return;
    }
    if (path === `/api/v1/audit-events/${ids.event}` && request.method() === "POST") {
      await fulfilJson(route, 200, {
        ...auditEvent(),
        details: { revokedFamilyCount: 1 },
      });
      return;
    }
    if (path === "/api/v1/users/list" && request.method() === "POST") {
      state.userListCalls += 1;
      if (state.forceUserListUnauthorized) {
        state.unauthorizedUserListCalls += 1;
        state.forceRefreshUnauthorized = true;
        await fulfilProblem(route, 401, "access-token-expired");
        return;
      }
      if (state.forceUserListError) {
        await fulfilProblem(route, 503, "users-temporarily-unavailable");
        return;
      }
      if (scenario === "user") {
        await fulfilProblem(route, 403, "forbidden");
        return;
      }

      const currentActor = currentUser(scenario);
      const actorIdentity: MockUser = {
        id: currentActor.id,
        account: currentActor.account,
        displayName: currentActor.displayName,
        role: currentActor.role,
        status: currentActor.status,
        version: currentActor.version,
        lastLoginAt: currentActor.lastLoginAt,
        deletedAt: currentActor.deletedAt,
        createdAt: currentActor.createdAt,
        updatedAt: currentActor.updatedAt,
      };
      const visibleUsers = [
        actorIdentity,
        ...(scenario === "super"
          ? state.users
          : state.users.filter((user) => user.role === "USER")),
      ];
      const requestedStatus = url.searchParams.get("status");
      const filteredUsers =
        requestedStatus === null
          ? visibleUsers.filter((user) => user.status !== "DELETED")
          : visibleUsers.filter((user) => user.status === requestedStatus);
      const requestedSearch = url.searchParams.get("q")?.trim().toLowerCase();
      const searchedUsers =
        requestedSearch === undefined || requestedSearch.length === 0
          ? filteredUsers
          : filteredUsers.filter(
              (user) =>
                user.account.toLowerCase().includes(requestedSearch) ||
                user.displayName.toLowerCase().includes(requestedSearch),
            );
      await fulfilJson(route, 200, { items: searchedUsers, page: { nextCursor: null } });
      return;
    }
    if (path === "/api/v1/users" && request.method() === "POST") {
      const input = requestJson(route);
      const createdUser: MockUser = {
        id: ids.created,
        account: typeof input.account === "string" ? input.account : "new.user",
        displayName: typeof input.displayName === "string" ? input.displayName : "新用户",
        role: input.role === "ADMIN" && scenario === "super" ? "ADMIN" : "USER",
        status: input.status === "DISABLED" ? "DISABLED" : "ACTIVE",
        version: 1,
        ...timestamps(),
      };
      state.users = [...state.users, createdUser];
      await fulfilJson(route, 201, createdUser, { ETag: '"user-created-v1"' });
      return;
    }
    if (path.endsWith("/password-reset") && request.method() === "POST") {
      await route.fulfill({ status: 204 });
      return;
    }

    const userMatch = path.match(/^\/api\/v1\/users\/([0-9a-f-]+)(?:\/(update|delete))?$/i);
    if (userMatch?.[1] !== undefined) {
      const user = state.users.find((candidate) => candidate.id === userMatch[1]);
      if (user === undefined) {
        await fulfilProblem(route, 404, "not-found");
        return;
      }
      if (userMatch[2] === undefined) {
        await fulfilJson(route, 200, user, { ETag: `"${user.id}-v${user.version}"` });
        return;
      }
      if (userMatch[2] === "update") {
        const input = requestJson(route);
        const updatedUser: MockUser = {
          ...user,
          displayName: typeof input.displayName === "string" ? input.displayName : user.displayName,
          role:
            input.role === "ADMIN" && scenario === "super"
              ? "ADMIN"
              : input.role === "USER"
                ? "USER"
                : user.role,
          status:
            input.status === "DISABLED"
              ? "DISABLED"
              : input.status === "ACTIVE"
                ? "ACTIVE"
                : user.status,
          version: user.version + 1,
          updatedAt: "2026-07-26T10:00:00.000Z",
        };
        state.users = state.users.map((candidate) =>
          candidate.id === user.id ? updatedUser : candidate,
        );
        await fulfilJson(route, 200, updatedUser, {
          ETag: `"${updatedUser.id}-v${updatedUser.version}"`,
        });
        return;
      }
      if (userMatch[2] === "delete") {
        state.users = state.users.filter((candidate) => candidate.id !== user.id);
        await route.fulfill({ status: 204 });
        return;
      }
    }

    await fulfilProblem(route, 404, "not-found");
  });

  return state;
}

/** 为指定角色填充并提交包含常驻验证码的完整登录表单。 */
async function login(page: Page, account: "user.demo" | "admin.demo" | "super.demo") {
  await page.goto("/login");
  await expect(page.getByRole("img", { name: "图形验证码" })).toBeVisible();
  await page.getByLabel("账号").fill(account);
  await page.getByRole("textbox", { name: "密码" }).fill("secure-pass-123");
  await page.getByRole("textbox", { name: "验证码" }).fill("1234");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page).toHaveURL(/\/$/);
  await page.context().addCookies([
    {
      name: "e2e_session",
      value: account,
      url: "http://127.0.0.1:4173",
      httpOnly: true,
      sameSite: "Lax",
    },
  ]);
}

/** 断言默认桌面视口与文档级横向溢出边界。 */
async function expectDesktopLayout(page: Page) {
  expect(page.viewportSize()).toEqual({ width: 1440, height: 900 });
  await expect(page.locator("body")).toHaveCSS("min-width", "1200px");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
    ),
  ).toBe(true);
}

/** 验证表单顶部留出浮动标签向上偏移所需的完整空间。 */
async function expectFloatingLabelTopClearance(dialog: Locator) {
  const formFields = dialog.locator(".MuiDialogContent-root > .MuiStack-root");

  await expect(formFields).toHaveCount(1);
  await expect(formFields).toHaveCSS("padding-top", "12px");
}

/** 验证匿名访客始终获得浅色、常驻验证码且无冗余辅助文案的登录页。 */
/** 验证匿名用户只能访问始终启用验证码的登录页。 */
test("anonymous visitors see only the always-on CAPTCHA login page", async ({ page }) => {
  await installMockApi(page);

  // 模拟旧版深色偏好缓存，确保产品仍强制使用批准的浅色主题。
  await page.addInitScript(() => {
    window.localStorage.setItem("apex-data-intelligence:color-mode:v1", "dark");
  });
  await page.goto("/users");

  await expect(page).toHaveURL(/\/login\?returnTo=/);
  await expect(page.getByText("Apex数据智能分析平台")).toBeVisible();
  await expect(page.getByRole("heading", { name: /看见数据.*读懂市场/ })).toBeVisible();
  await expect(page.getByPlaceholder("请输入账号", { exact: true })).toBeVisible();
  await expect(page.getByPlaceholder("请输入密码", { exact: true })).toBeVisible();
  await expect(page.getByRole("img", { name: "图形验证码" })).toBeVisible();
  await expect(page.getByText("5–32 位")).toHaveCount(0);
  await expect(page.getByText("语音验证码")).toHaveCount(0);
  await expect(page.getByText("首次登录")).toHaveCount(0);
  await expect(page.getByText("请刷新验证码后继续。")).toHaveCount(0);
  await expect(page.getByText("系统不开放注册")).toHaveCount(0);
  await expect(page.getByText("quant-v2", { exact: false })).toHaveCount(0);
  expect(
    await page.locator("html").evaluate((element) => getComputedStyle(element).colorScheme),
  ).toBe("light");
  await expectDesktopLayout(page);
});

/** 验证普通用户在受限列表请求发出前看到权限拒绝页。 */
test("USER direct user-management access renders 403 before any restricted list data", async ({
  page,
}) => {
  const apiState = await installMockApi(page);

  await login(page, "user.demo");
  await page.goto("/users");

  await expect(page.getByRole("heading", { name: "无权访问此功能" })).toBeVisible();
  await expect(page.getByRole("table", { name: "用户列表" })).toHaveCount(0);
  expect(apiState.userListCalls).toBe(0);
  await expectDesktopLayout(page);
});

/** 验证受保护请求失效后清除业务外壳，并保留安全返回地址。 */
test("a protected 401 replaces the shell with login while preserving a safe return target", async ({
  page,
}) => {
  const apiState = await installMockApi(page);

  await login(page, "admin.demo");
  apiState.forceUserListUnauthorized = true;
  // 捕获 POST-only 用户列表请求，验证会话失效响应。
  const listResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/users/list" &&
      response.request().method() === "POST",
  );
  await page.goto("/users?status=ACTIVE");

  await expect.poll(() => apiState.userListCalls).toBeGreaterThan(0);
  await expect.poll(() => apiState.unauthorizedUserListCalls).toBeGreaterThan(0);
  expect((await listResponse).status()).toBe(401);
  await expect(page).toHaveURL(
    /\/login\?returnTo=%2Fusers%3Fstatus%3DACTIVE&reason=session-expired/,
  );
  await expect(page.getByRole("heading", { name: "登录" })).toBeVisible();
  await expect(page.getByText("登录状态已失效，请重新登录。验证后将返回原页面。")).toBeVisible();
  await expect(page.getByRole("table", { name: "用户列表" })).toHaveCount(0);
  await expectDesktopLayout(page);
});

/** 验证空态与可恢复错误提供明确动作，且不会丢弃已加载数据。 */
test("empty and recoverable error states provide clear next actions without discarding loaded rows", async ({
  page,
}) => {
  const apiState = await installMockApi(page);

  await login(page, "admin.demo");
  await page.goto("/users");
  await expect(page.getByRole("row").filter({ hasText: "market.user" })).toBeVisible();

  await page.getByRole("textbox", { name: "搜索" }).fill("not-found");
  await expect(page.getByText("没有匹配用户")).toBeVisible();
  await expect(page.getByText("调整筛选条件后重试。")).toBeVisible();
  await page.getByRole("button", { name: "重置筛选" }).click();
  await expect(page.getByRole("row").filter({ hasText: "market.user" })).toBeVisible();

  apiState.forceUserListError = true;
  await page.getByRole("button", { name: "刷新用户列表" }).click();
  await expect(page.getByText("用户列表暂时不可用，请稍后重试。")).toBeVisible();
  await expect(page.getByRole("row").filter({ hasText: "market.user" })).toBeVisible();

  apiState.forceUserListError = false;
  await page.getByRole("button", { name: "重试" }).click();
  await expect(page.getByText("用户列表暂时不可用，请稍后重试。")).toHaveCount(0);
  await expectDesktopLayout(page);
});

/** 验证管理员通过标准 Dialog 管理普通用户，且密码字段不会残留。 */
test("ADMIN manages USER targets through 720px Dialogs without retaining password fields", async ({
  page,
}) => {
  const apiState = await installMockApi(page);

  await login(page, "admin.demo");
  await expect(page.getByRole("link", { name: "Apex数据智能分析平台首页" })).toBeVisible();
  const primaryNavigation = page.getByRole("navigation", { name: "主导航" });
  await expect(primaryNavigation.getByText("工作区", { exact: true })).toBeVisible();
  await expect(primaryNavigation.getByText("系统管理", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "全局搜索功能即将开放" })).toBeVisible();
  await expect(page.getByText("数据分析工作区", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "平台工作台" })).toBeVisible();
  await page.goto("/users");

  await expect(page.getByRole("heading", { name: "用户管理" })).toBeVisible();
  const userFilters = page.getByRole("region", { name: "用户筛选" });
  await expect(userFilters.getByRole("combobox")).toHaveCount(2);
  await expect(page.getByRole("button", { name: "切换排序方向" })).toHaveCount(0);
  const usersTable = page.getByRole("table", { name: "用户列表" });
  const createdAtSort = usersTable.getByRole("button", {
    name: "按创建时间排序，当前降序",
  });
  await expect(createdAtSort).toHaveCount(1);
  await createdAtSort.click();
  await expect(page).toHaveURL(/\/users\?sort=createdAt&order=asc/);
  await usersTable.getByRole("button", { name: "按创建时间排序，当前升序" }).click();
  await expect(page).toHaveURL(/\/users\?sort=createdAt&order=desc/);
  const displayNameSort = usersTable.getByRole("button", { name: "按用户排序，当前未排序" });
  await displayNameSort.click();
  await expect(page).toHaveURL(/\/users\?sort=displayName&order=asc/);
  await expect(usersTable.getByRole("button", { name: "按更新时间排序，当前未排序" })).toHaveCount(
    1,
  );
  const listCallsBeforeRefresh = apiState.userListCalls;
  await page.getByRole("button", { name: "刷新用户列表" }).click();
  await expect.poll(() => apiState.userListCalls).toBeGreaterThan(listCallsBeforeRefresh);
  await page.getByRole("button", { name: "新建用户" }).click();
  const createDialog = page.getByRole("dialog", { name: "新建用户" });
  await expect(createDialog).toBeVisible();
  await expect(createDialog.getByLabel("账号")).toHaveJSProperty("required", true);
  await expectFloatingLabelTopClearance(createDialog);
  const createDialogBox = await page.locator(".MuiDialog-paper").boundingBox();
  expect(createDialogBox?.width).toBeCloseTo(720, 0);
  await createDialog.getByLabel("账号").fill("abcd");
  await createDialog.getByLabel("姓名").fill("新增用户");
  await createDialog.getByLabel("初始密码").fill("secure-pass-123");
  await createDialog.getByRole("button", { name: "保存" }).click();
  await expect(
    createDialog.getByText("账号需为 5–32 位小写字母、数字、点、下划线或连字符。"),
  ).toBeVisible();
  await expect(createDialog.getByLabel("初始密码")).toHaveValue("");
  await createDialog.getByLabel("账号").fill("market.new");
  await createDialog.getByLabel("初始密码").fill("secure-pass-123");
  await createDialog.getByRole("button", { name: "保存" }).click();
  await expect(createDialog).toHaveCount(0);
  await expect(page.getByText("用户已创建。")).toBeVisible();

  await page.getByRole("button", { name: "编辑用户" }).first().click();
  const editDialog = page.getByRole("dialog", { name: "编辑用户" });
  await expect(editDialog).toBeVisible();
  await expect(editDialog.getByLabel("初始密码")).toHaveCount(0);
  await expect(editDialog.getByLabel("账号")).toHaveJSProperty("readOnly", true);
  await expect(editDialog.getByLabel("账号")).toHaveJSProperty("required", false);
  await expectFloatingLabelTopClearance(editDialog);
  await editDialog.getByLabel("姓名").fill("已更新用户");
  await editDialog.getByRole("button", { name: "保存" }).click();
  await expect(page.getByText("用户信息已保存。")).toBeVisible();

  await page.goto(`/users?dialog=delete&userId=${ids.user}`);
  const deleteDialog = page.getByRole("dialog", { name: "删除用户" });
  await expect(deleteDialog).toBeVisible();
  const deleteDialogBox = await page.locator(".MuiDialog-paper").boundingBox();
  expect(deleteDialogBox?.width).toBeCloseTo(720, 0);
  await expect(
    deleteDialog.getByText(
      "管理员可删除普通用户；超级管理员可删除管理员和普通用户。删除后账号将立即无法登录，审计记录会保留。",
    ),
  ).toBeVisible();
  await expect(deleteDialog.getByText("market.user", { exact: true })).toBeVisible();
  await expect(deleteDialog.getByText("普通用户 · 当前启用", { exact: true })).toBeVisible();
  await expect(deleteDialog.getByRole("button", { name: "确认删除" })).toBeVisible();
  await deleteDialog.getByRole("button", { name: "取消" }).click();
  await expectDesktopLayout(page);
});

/** 验证超级管理员只读查看已删除用户，并可维护管理员角色。 */
test("SUPER_ADMIN keeps deleted users read-only and can manage administrator roles", async ({
  page,
}) => {
  await installMockApi(page);

  await login(page, "super.demo");
  await expect(page.getByRole("button", { name: "打开用户菜单" })).toContainText("超级管理员");
  await page.goto("/users?status=DELETED");
  const deletedRow = page.getByRole("row").filter({ hasText: "archived.user" });
  await expect(deletedRow).toBeVisible();
  await expect(deletedRow.getByRole("button", { name: "编辑用户" })).toHaveCount(0);
  await expect(deletedRow.getByRole("button", { name: "重置密码" })).toHaveCount(0);
  await expect(deletedRow.getByRole("button", { name: "删除用户" })).toHaveCount(0);

  await page.goto("/users");
  const selfRow = page.getByRole("row").filter({ hasText: "super.demo" });
  await expect(selfRow).toBeVisible();
  await expect(selfRow.getByText("当前账号")).toBeVisible();
  await expect(selfRow.getByRole("button", { name: "编辑用户" })).toHaveCount(0);
  await expect(selfRow.getByRole("button", { name: "重置密码" })).toHaveCount(0);
  await expect(selfRow.getByRole("button", { name: "删除用户" })).toHaveCount(0);
  await page.getByRole("button", { name: "新建用户" }).click();
  const createDialog = page.getByRole("dialog", { name: "新建用户" });
  await createDialog.getByLabel("角色").click();
  await expect(page.getByRole("option", { name: "管理员" })).toBeVisible();
  await page.keyboard.press("Escape");
  await createDialog.getByRole("button", { name: "取消" }).click();
  await page.getByRole("button", { name: "打开用户菜单" }).click();
  await expect(page.getByRole("menuitem", { name: "退出登录" })).toBeVisible();
  await expectDesktopLayout(page);
});

/** 验证个人中心资料、Session 与改密形成真实 API 闭环。 */
test("all account-security actions stay POST-only and password change clears the session", async ({
  page,
}) => {
  const apiState = await installMockApi(page);

  await login(page, "super.demo");
  await page.goto("/account");
  await expect(page.getByRole("heading", { name: "我的账户" })).toBeVisible();
  await expect(page.getByRole("table", { name: "活动会话" })).toBeVisible();
  await expect(page.getByText("会话 9013")).toBeVisible();

  const displayName = page.getByLabel("显示名称");
  apiState.forceProfileConflict = true;
  await displayName.fill("平台负责人");
  await page.getByRole("button", { name: "保存资料" }).click();
  await expect(
    page.getByText("资料已在其他窗口更新。当前草稿仍保留，请重新加载后再确认。"),
  ).toBeVisible();
  await expect(displayName).toHaveValue("平台负责人");
  await page.getByRole("button", { name: "重新加载" }).click();
  await expect(displayName).toHaveValue("超级管理员");
  await displayName.fill("平台负责人");
  await page.getByRole("button", { name: "保存资料" }).click();
  await expect(page.getByText("个人资料已保存。")).toBeVisible();
  await expect(page.getByRole("button", { name: "打开用户菜单" })).toContainText("平台负责人");

  await page.getByRole("button", { name: "退出其他会话" }).click();
  const revokeDialog = page.getByRole("dialog", { name: "退出其他会话？" });
  await expect(revokeDialog).toContainText("将保留当前会话");
  await revokeDialog.getByRole("button", { name: "确认退出" }).click();
  await expect(page.getByText("已退出 1 个其他会话。")).toBeVisible();

  await page.getByRole("button", { name: "修改" }).click();
  const passwordDialog = page.getByRole("dialog", { name: "修改登录密码" });
  await passwordDialog.getByLabel("当前密码").fill("secure-pass-123");
  await passwordDialog.getByLabel("新密码").fill("secure-pass-456");
  await passwordDialog.getByRole("button", { name: "确认修改" }).click();
  await expect(page).toHaveURL(/\/login\?reason=password-changed/);
  await expect(page.getByRole("heading", { name: "登录" })).toBeVisible();
  await expect(page.getByText("密码已修改，请使用新密码重新登录。")).toBeVisible();
  await expectDesktopLayout(page);
});

/** 验证审计路由默认拒绝非超级管理员，且 SUPER_ADMIN 可重访筛选与详情。 */
test("audit route guards before fetch and preserves URL-owned filters and detail", async ({
  page,
}) => {
  const apiState = await installMockApi(page);

  await login(page, "admin.demo");
  await page.goto("/security/audit");
  await expect(page.getByRole("heading", { name: "无权访问此功能" })).toBeVisible();
  expect(apiState.auditListCalls).toBe(0);

  await page.getByRole("button", { name: "打开用户菜单" }).click();
  await page.getByRole("menuitem", { name: "退出登录" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await login(page, "super.demo");
  await page.goto("/security/audit");
  await expect(page.getByRole("heading", { name: "安全审计" })).toBeVisible();
  await expect(page.getByRole("table", { name: "审计事件" })).toBeVisible();

  await page.getByLabel("Actor ID").fill(ids.super);
  await page.getByRole("button", { name: "应用筛选" }).click();
  await expect(page).toHaveURL(new RegExp(`actorId=${ids.super}`));
  await page.getByRole("button", { name: "查看详情" }).click();
  await expect(page).toHaveURL(new RegExp(`eventId=${ids.event}`));
  const drawer = page.getByRole("presentation").filter({ hasText: "审计事件详情" });
  await expect(drawer.getByText("检测到 Refresh 重放")).toBeVisible();
  await expect(drawer.getByText("auth.refresh.replay_detected")).toBeVisible();
  await page.getByRole("button", { name: "关闭审计详情" }).click();
  await expect(page).not.toHaveURL(/eventId=/);
  expect(apiState.auditListCalls).toBeGreaterThan(0);
  await expectDesktopLayout(page);
});
