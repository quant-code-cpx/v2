import { afterEach, beforeEach, describe, expect, it } from "vite-plus/test";

import {
  cancelDataSync,
  getDataOperationSubmission,
  getDataOperationsOverview,
  getDataSyncCommand,
  getDataSyncRun,
  getDatasetHealthCheck,
  getDatasetHealthEvaluation,
  getOperationalDataset,
  preflightDataSync,
  retryDataSync,
  searchDataOperations,
  searchDataSyncRuns,
  searchDataSyncSchedules,
  searchDatasetHealthEvaluations,
  searchOperationalDatasets,
  setDataSyncScheduleEnabled,
  submitDataSync,
  submitDatasetHealthCheck,
  upsertDataSyncSchedule,
} from "../data-operations";
import { authSession } from "../auth-session";
import { setHttpTransportForTests } from "../http";
import type { HttpTransportRequest } from "../http";

/** 固定的非敏感标识，便于逐项断言公开合同请求体。 */
const identifiers = {
  dataset: "market.daily_bar",
  command: "command-001",
  run: "run-001",
  healthCheck: "health-check-001",
  evaluation: "evaluation-001",
  schedule: "schedule-001",
  submission: "submission-001",
  request: "request-001",
  user: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
} as const;

/** 返回登录和身份验证流程所需的最小超级管理员公开投影。 */
function currentUserPayload() {
  return {
    id: identifiers.user,
    account: "super.demo",
    displayName: "超级管理员",
    role: "SUPER_ADMIN",
    status: "ACTIVE",
    version: 1,
    lastLoginAt: null,
    deletedAt: null,
    createdAt: "2026-07-29T00:00:00.000Z",
    updatedAt: "2026-07-29T00:00:00.000Z",
    permissions: [],
  };
}

/** 构造传输层可识别的 JSON 响应。 */
function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** 解析共享传输层已序列化的 JSON 请求体。 */
function requestBody(request: HttpTransportRequest): unknown {
  if (typeof request.init.body !== "string") throw new TypeError("请求体必须为 JSON 字符串。");
  return JSON.parse(request.init.body) as unknown;
}

/** 通过真实内存会话取得测试使用的 Bearer access token。 */
async function establishSession(): Promise<void> {
  await authSession.login({
    account: "super.demo",
    password: "secure-pass-123",
    captchaId: identifiers.user,
    captchaAnswer: "1234",
  });
}

describe("data operations API", () => {
  /** 每个测试从没有 access token 的安全状态开始。 */
  beforeEach(() => {
    authSession.clear();
  });

  /** 每个测试恢复浏览器传输并清除内存中的身份与查询数据。 */
  afterEach(() => {
    setHttpTransportForTests();
    authSession.clear();
  });

  /** 19 条公开数据运维适配器只能使用 Bearer 的 POST 边界和合同请求体。 */
  it("maps all 19 public POST routes with bearer authentication and stable write headers", async () => {
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
      if (path === "/api/v1/users/me") return jsonResponse(200, currentUserPayload());
      return jsonResponse(200, {});
    });
    await establishSession();

    const target = {
      datasetCode: identifiers.dataset,
      mode: "INCREMENTAL" as const,
      selector: { kind: "GLOBAL" as const },
      dateFrom: null,
      dateTo: null,
      observationDate: null,
    };
    const writeOptions = {
      idempotencyKey: "idempotency-001",
      requestId: identifiers.request,
    };

    await getDataOperationsOverview();
    await searchOperationalDatasets({ query: "日线", limit: 20 });
    await getOperationalDataset(identifiers.dataset);
    await preflightDataSync({ targets: [target] });
    await submitDataSync(
      {
        preflightId: "preflight-001",
        requestHash: "hash-001",
        targets: [target],
        reason: "补齐日线数据",
      },
      writeOptions,
    );
    await cancelDataSync(
      { target: { resourceType: "RUN", resourceId: identifiers.run }, reason: "运营取消" },
      writeOptions,
    );
    await retryDataSync(
      {
        target: { resourceType: "COMMAND", resourceId: identifiers.command },
        reason: "可重试失败",
      },
      writeOptions,
    );
    await getDataSyncCommand(identifiers.command);
    await searchDataSyncRuns({ datasetCodes: [identifiers.dataset], limit: 20 });
    await getDataSyncRun({
      runId: identifiers.run,
      partitionsCursor: "partitions-001",
      timelineCursor: "timeline-001",
    });
    await searchDatasetHealthEvaluations({ datasetCodes: [identifiers.dataset], limit: 20 });
    await getDatasetHealthEvaluation({
      evaluationId: identifiers.evaluation,
      issuesCursor: "issues-001",
    });
    await submitDatasetHealthCheck(
      { targets: [{ datasetCode: identifiers.dataset, dataVersion: null }], reason: "例行复核" },
      writeOptions,
    );
    await getDatasetHealthCheck(identifiers.healthCheck);
    await searchDataSyncSchedules({ datasetCodes: [identifiers.dataset], limit: 20 });
    await upsertDataSyncSchedule(
      {
        scheduleId: null,
        datasetCode: identifiers.dataset,
        mode: "INCREMENTAL",
        selector: { kind: "GLOBAL" },
        targetPolicy: { policyVersion: 1, dateResolution: "SCHEDULED_LOCAL_DATE" },
        frequency: {
          kind: "DAILY",
          timezone: "Asia/Shanghai",
          localTime: "18:00",
          dayOfWeek: null,
          dayOfMonth: null,
          intervalMinutes: null,
          calendarCode: null,
        },
        misfirePolicy: "RUN_ONCE",
        coalesce: true,
        enabled: true,
        expectedVersion: null,
        reason: "创建日线计划",
      },
      writeOptions,
    );
    await setDataSyncScheduleEnabled(
      {
        scheduleId: identifiers.schedule,
        enabled: false,
        expectedVersion: 1,
        reason: "维护窗口暂停",
      },
      writeOptions,
    );
    await getDataOperationSubmission(identifiers.submission);
    await searchDataOperations({ deliveryStatuses: ["PENDING"], limit: 20 });

    const dataOperationsRequests = requests.filter((request) =>
      new URL(request.url, "http://apex.local").pathname.startsWith("/api/v1/data-operations/"),
    );
    const actualPaths = dataOperationsRequests.map(
      (request) => new URL(request.url, "http://apex.local").pathname,
    );
    expect(actualPaths).toEqual([
      "/api/v1/data-operations/overview",
      "/api/v1/data-operations/datasets/search",
      "/api/v1/data-operations/datasets/detail",
      "/api/v1/data-operations/sync/preflight",
      "/api/v1/data-operations/sync/submit",
      "/api/v1/data-operations/sync/cancel",
      "/api/v1/data-operations/sync/retry",
      "/api/v1/data-operations/commands/detail",
      "/api/v1/data-operations/runs/search",
      "/api/v1/data-operations/runs/detail",
      "/api/v1/data-operations/health/evaluations/search",
      "/api/v1/data-operations/health/evaluations/detail",
      "/api/v1/data-operations/health/checks/submit",
      "/api/v1/data-operations/health/checks/detail",
      "/api/v1/data-operations/schedules/search",
      "/api/v1/data-operations/schedules/upsert",
      "/api/v1/data-operations/schedules/set-enabled",
      "/api/v1/data-operations/submissions/detail",
      "/api/v1/data-operations/operations/search",
    ]);
    expect(dataOperationsRequests).toHaveLength(19);
    expect(dataOperationsRequests.every((request) => request.init.method === "POST")).toBe(true);
    expect(
      dataOperationsRequests.every(
        (request) =>
          new Headers(request.init.headers).get("Authorization") === "Bearer access-super",
      ),
    ).toBe(true);

    const preflightRequest = dataOperationsRequests[3];
    const runDetailRequest = dataOperationsRequests[9];
    const scheduleUpsertRequest = dataOperationsRequests[15];
    if (
      preflightRequest === undefined ||
      runDetailRequest === undefined ||
      scheduleUpsertRequest === undefined
    ) {
      throw new Error("19 条公开数据运维请求未完整生成。");
    }
    expect(requestBody(preflightRequest)).toEqual({ targets: [target] });
    expect(requestBody(runDetailRequest)).toEqual({
      runId: identifiers.run,
      partitionsCursor: "partitions-001",
      timelineCursor: "timeline-001",
    });
    expect(requestBody(scheduleUpsertRequest)).toMatchObject({
      datasetCode: identifiers.dataset,
      selector: { kind: "GLOBAL" },
    });

    const writePaths = new Set([
      "/api/v1/data-operations/sync/submit",
      "/api/v1/data-operations/sync/cancel",
      "/api/v1/data-operations/sync/retry",
      "/api/v1/data-operations/health/checks/submit",
      "/api/v1/data-operations/schedules/upsert",
      "/api/v1/data-operations/schedules/set-enabled",
    ]);
    dataOperationsRequests
      .filter((request) => writePaths.has(new URL(request.url, "http://apex.local").pathname))
      .forEach((request) => {
        const headers = new Headers(request.init.headers);
        expect(headers.get("Idempotency-Key")).toBe(writeOptions.idempotencyKey);
        expect(headers.get("X-Request-Id")).toBe(identifiers.request);
      });
  });
});
