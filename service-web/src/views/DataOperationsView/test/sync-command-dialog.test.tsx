import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";

import { authSession } from "../../../api/auth-session";
import { setHttpTransportForTests } from "../../../api/http";
import { SyncCommandDialog } from "../components/SyncCommandDialog";
import type { DatasetSummary, SubmissionReceipt } from "../../../types/data-operations";
import type { HttpTransportRequest } from "../../../api/http";

/** 构造一个只暴露服务端声明 selector kind 的安全目录摘要。 */
function dataset(
  datasetCode: string,
  selectorKinds: DatasetSummary["capability"]["selectorKinds"] = ["GLOBAL"],
): DatasetSummary {
  return {
    datasetCode,
    displayName: datasetCode,
    domain: "市场行情",
    lifecycleStatus: "PRODUCTION",
    availability: "ENABLED",
    availabilityReasonCode: null,
    observationState: "PRESENT",
    observationStateReasonCode: null,
    sourceBindings: [],
    capability: {
      supportedModes: ["INCREMENTAL"],
      scheduleSupportedModes: ["INCREMENTAL"],
      scheduleTargetPolicyOptions: [],
      selectorKinds,
      maxRangeDays: null,
      scheduleEligible: false,
      manualEnabled: true,
      correctionLookbackDays: 0,
    },
    timing: {
      lastAttemptStartedAt: null,
      lastAttemptFinishedAt: null,
      lastAttemptStatus: null,
      lastSuccessAt: null,
      lastPublishedAt: null,
      dataAsOf: null,
      dataAsOfKind: "NOT_APPLICABLE",
      dataAsOfLabel: "—",
      coverageFrom: null,
      coverageTo: null,
      freshnessStatus: "NOT_APPLICABLE",
      freshnessLagValue: null,
      freshnessLagUnit: null,
      freshnessReasonCode: null,
      freshnessEvaluatedAt: "2026-07-29T00:00:00.000Z",
    },
    latestRun: null,
    healthSummary: {
      status: "UNKNOWN",
      score: null,
      evaluatedAt: null,
      evaluationId: null,
      warningCount: 0,
      criticalCount: 0,
      openIssueCount: 0,
      affectedRecordCount: null,
    },
    scheduleSummary: null,
  };
}

/** 构造数据运维写入初始固定为 PENDING 的公开回执。 */
function pendingReceipt(): SubmissionReceipt {
  return {
    submissionId: "submission-001",
    action: "SYNC_SUBMIT",
    deliveryStatus: "PENDING",
    operationResult: "UNKNOWN",
    authorityResource: null,
    queuePosition: null,
    authorizedAt: "2026-07-29T00:00:00.000Z",
    updatedAt: "2026-07-29T00:00:00.000Z",
    requestId: "request-001",
    error: null,
  };
}

/** 通过真实会话协调器建立仅在内存存在的超级管理员 access token。 */
async function establishSession(): Promise<void> {
  await authSession.login({
    account: "super.demo",
    password: "secure-pass-123",
    captchaId: "captcha-001",
    captchaAnswer: "1234",
  });
}

/** 从共享传输层提取某个公开 API 路径的最后一个请求。 */
function findRequest(requests: HttpTransportRequest[], path: string): HttpTransportRequest {
  const request = requests.find(
    (candidate) => new URL(candidate.url, "http://apex.local").pathname === path,
  );
  if (request === undefined) throw new Error(`未找到请求 ${path}`);
  return request;
}

/** 解析共享 POST 传输层生成的 JSON 请求体。 */
function requestBody(request: HttpTransportRequest): unknown {
  if (typeof request.init.body !== "string") throw new TypeError("请求体必须为 JSON 字符串。");
  return JSON.parse(request.init.body) as unknown;
}

describe("SyncCommandDialog", () => {
  /** 每个测试前清空全局内存 token。 */
  beforeEach(() => {
    authSession.clear();
  });

  /** 每个测试后恢复传输实现并丢弃认证状态。 */
  afterEach(() => {
    setHttpTransportForTests();
    authSession.clear();
    cleanup();
  });

  /** 批量预检冻结服务端目标顺序，提交先得到 PENDING 回执而不假称同步已开始。 */
  it("preflights ordered targets then submits the frozen snapshot as PENDING", async () => {
    const requests: HttpTransportRequest[] = [];
    const targets = [
      dataset("fund.etf.profile.reported", ["ETF"]),
      dataset("fund.etf.bar.1d.reported", ["ETF"]),
    ];
    const frozenProfileDataVersions = {
      SSE: "00000000-0000-4000-8000-000000000011",
      SZSE: "00000000-0000-4000-8000-000000000012",
    };
    setHttpTransportForTests(async (request) => {
      requests.push(request);
      const path = new URL(request.url, "http://apex.local").pathname;
      if (path === "/api/v1/auth/login") {
        return new Response(
          JSON.stringify({
            accessToken: "access-super",
            accessTokenExpiresIn: 600,
            user: {
              id: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
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
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (path === "/api/v1/users/me") {
        return new Response(
          JSON.stringify({
            id: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
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
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (path === "/api/v1/data-operations/sync/preflight") {
        const body = requestBody(request) as {
          targets: Array<{
            datasetCode: string;
            selector: Record<string, unknown>;
          }>;
        };
        return new Response(
          JSON.stringify({
            preflightId: "preflight-001",
            requestHash: "hash-001",
            expiresAt: "2026-07-29T01:00:00.000Z",
            queueDepth: 2,
            executionSlot: {
              state: "IDLE",
              runId: null,
              datasetCode: null,
              leaseUntil: null,
              heartbeatAt: null,
            },
            targets: body.targets.map((target) => ({
              target:
                target.selector.scope === "ALL_ETFS"
                  ? {
                      ...target,
                      selector: {
                        ...target.selector,
                        profileDataVersions: frozenProfileDataVersions,
                      },
                    }
                  : target,
              eligible: true,
              estimatedPartitions: 1,
              estimatedProviderCalls: 1,
              resolvedDateFrom: null,
              resolvedDateTo: null,
              warnings: [],
            })),
            accepted: true,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (path === "/api/v1/data-operations/sync/submit") {
        return new Response(JSON.stringify(pendingReceipt()), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify({}), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    });
    await establishSession();
    const onSubmission = vi.fn();
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={client}>
        <SyncCommandDialog open datasets={targets} onClose={vi.fn()} onSubmission={onSubmission} />
      </QueryClientProvider>,
    );

    const etfCapabilities = screen.getAllByLabelText("ETF 数据能力");
    expect(etfCapabilities[0]).toHaveValue("MASTER");
    expect(etfCapabilities[1]).toHaveValue("BARS");
    expect(screen.getByLabelText("主数据同步范围")).toHaveTextContent("沪深全市场");
    await user.click(screen.getByLabelText("ETF 同步范围"));
    await user.click(await screen.findByRole("option", { name: "全部已发布 ETF" }));
    await user.click(screen.getByRole("button", { name: "执行预检" }));
    await screen.findByText("预检结果");
    expect(screen.getByText(/BARS\.全部已发布 ETF/)).toBeInTheDocument();
    expect(requestBody(findRequest(requests, "/api/v1/data-operations/sync/preflight"))).toEqual({
      targets: [
        {
          datasetCode: "fund.etf.profile.reported",
          mode: "INCREMENTAL",
          selector: {
            kind: "ETF",
            operation: "MASTER",
            venue: null,
            scope: "ALL_VENUES",
            etf: null,
          },
          dateFrom: null,
          dateTo: null,
          observationDate: null,
        },
        {
          datasetCode: "fund.etf.bar.1d.reported",
          mode: "INCREMENTAL",
          selector: {
            kind: "ETF",
            operation: "BARS",
            venue: null,
            scope: "ALL_ETFS",
            etf: null,
            profileDataVersions: null,
          },
          dateFrom: null,
          dateTo: null,
          observationDate: null,
        },
      ],
    });

    await user.type(screen.getByRole("textbox", { name: "操作原因" }), "补齐两个数据集");
    await user.click(screen.getByRole("button", { name: "提交同步意图（2）" }));

    await waitFor(() => expect(onSubmission).toHaveBeenCalledWith(pendingReceipt()));
    expect(requestBody(findRequest(requests, "/api/v1/data-operations/sync/submit"))).toMatchObject(
      {
        preflightId: "preflight-001",
        requestHash: "hash-001",
        targets: [
          {
            datasetCode: "fund.etf.profile.reported",
            selector: {
              kind: "ETF",
              operation: "MASTER",
              venue: null,
              scope: "ALL_VENUES",
              etf: null,
            },
          },
          {
            datasetCode: "fund.etf.bar.1d.reported",
            selector: {
              kind: "ETF",
              operation: "BARS",
              venue: null,
              scope: "ALL_ETFS",
              etf: null,
              profileDataVersions: frozenProfileDataVersions,
            },
          },
        ],
        reason: "补齐两个数据集",
      },
    );
  });
});
