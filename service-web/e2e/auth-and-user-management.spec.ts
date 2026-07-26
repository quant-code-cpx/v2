import { expect, test } from "@playwright/test";
import type { Locator, Page, Route } from "@playwright/test";

/** Use fixed valid UUIDs so URL-owned Dialog state follows target-contract validation. */
const ids = {
  user: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
  admin: "7ce0f18a-9f4d-4b3a-ae69-d0ff1707df91",
  super: "72a4d2a1-3798-4bcf-978f-75c69c6d246b",
  created: "8f401b48-5b0e-4a76-8d85-2c7101a28955",
  deleted: "9eb2c698-6401-4bd5-81b2-f3a7900ea87b",
} as const;

/** Represent test-only role scenarios accepted by the controlled API route handler. */
type Scenario = "user" | "admin" | "super";

/** Represent a non-sensitive user shape used only by strict contract test responses. */
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

/** Track test backend behavior so a USER 403 test can prove no list was fetched. */
interface MockApiState {
  users: MockUser[];
  userListCalls: number;
  unauthorizedUserListCalls: number;
  forceRefreshUnauthorized: boolean;
  forceUserListUnauthorized: boolean;
  forceUserListError: boolean;
}

/** Return contract-shaped timestamps shared by deterministic E2E response fixtures. */
function timestamps() {
  return {
    lastLoginAt: "2026-07-26T09:20:00.000Z",
    deletedAt: null,
    createdAt: "2026-07-26T08:00:00.000Z",
    updatedAt: "2026-07-26T09:20:00.000Z",
  };
}

/** Build a current identity matching one scenario's server-calculated permissions. */
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
        ? ["profile:read", "profile:update", "password:change"]
        : scenario === "admin"
          ? [
              "profile:read",
              "users:read",
              "users:create",
              "users:update",
              "users:delete",
              "users:reset-password",
            ]
          : [
              "profile:read",
              "users:read",
              "users:create",
              "users:update",
              "users:delete",
              "users:reset-password",
              "admins:create",
              "admins:manage",
            ],
  };
}

/** Read a scenario only from the test access token supplied by the in-memory Web client. */
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

/** Recover E2E refresh identity from a test-only HttpOnly-cookie stand-in after page reload. */
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

/** Fulfil a JSON API response with a no-store-compatible test body. */
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

/** Fulfil a stable Problem response without furnishing sensitive problem detail. */
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

/** Parse a JSON request body used only by the controlled E2E API adapter. */
function requestJson(route: Route): Record<string, unknown> {
  const rawBody = route.request().postData() ?? "{}";
  const parsedBody: unknown = JSON.parse(rawBody);

  return typeof parsedBody === "object" && parsedBody !== null
    ? (parsedBody as Record<string, unknown>)
    : {};
}

/** Install a strict Contract 0002 API adapter for one browser page; production code still uses fetch. */
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
  };

  /** Intercept only versioned API calls and return contractual shapes for UI acceptance tests. */
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
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
      await route.fulfill({ status: 204 });
      return;
    }
    if (path === "/api/v1/users/me" && request.method() === "GET") {
      await fulfilJson(route, 200, currentUser(scenario));
      return;
    }
    if (path === "/api/v1/users" && request.method() === "GET") {
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

    const userMatch = path.match(/^\/api\/v1\/users\/([0-9a-f-]+)$/i);
    if (userMatch?.[1] !== undefined) {
      const user = state.users.find((candidate) => candidate.id === userMatch[1]);
      if (user === undefined) {
        await fulfilProblem(route, 404, "not-found");
        return;
      }
      if (request.method() === "GET") {
        await fulfilJson(route, 200, user, { ETag: `"${user.id}-v${user.version}"` });
        return;
      }
      if (request.method() === "PATCH") {
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
      if (request.method() === "DELETE") {
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

test("a protected 401 replaces the shell with login while preserving a safe return target", async ({
  page,
}) => {
  const apiState = await installMockApi(page);

  await login(page, "admin.demo");
  apiState.forceUserListUnauthorized = true;
  const listResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/users" && response.request().method() === "GET",
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
  await expect(page.getByRole("heading", { name: "首页能力建设中" })).toBeVisible();
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
