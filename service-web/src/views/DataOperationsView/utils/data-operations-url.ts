import type { DatasetSearchRequest, HealthStatus, RunStatus } from "../../../types/data-operations";
import { dataOperationsTabs } from "./data-operations-presentation";
import type { DataOperationsTab } from "./data-operations-presentation";

/** 描述允许进入地址栏、可安全恢复的数据运维页面状态。 */
export interface DataOperationsUrlState {
  tab: DataOperationsTab;
  catalog: DatasetSearchRequest;
  runCursor?: string;
  healthCursor?: string;
  scheduleCursor?: string;
  operationCursor?: string;
  runStatus?: RunStatus;
  health?: HealthStatus;
  datasetCode?: string;
  runId?: string;
  commandId?: string;
  healthCheckId?: string;
  evaluationId?: string;
  scheduleId?: string;
  submissionId?: string;
}

/** 固定默认 URL 状态，避免未验证 URL 参数影响合同请求。 */
const defaultState: DataOperationsUrlState = {
  tab: "datasets",
  catalog: { limit: 50 },
};

/** 只接受合同允许的 Tab，其他值回退至数据资产。 */
function parseTab(value: string | null): DataOperationsTab {
  return dataOperationsTabs.includes(value as DataOperationsTab)
    ? (value as DataOperationsTab)
    : "datasets";
}

/** 只接受合同允许的 run 状态筛选。 */
function parseRunStatus(value: string | null): RunStatus | undefined {
  const values: RunStatus[] = [
    "QUEUED",
    "RUNNING",
    "CANCEL_REQUESTED",
    "SUCCEEDED",
    "PARTIAL",
    "FAILED",
    "CANCELLED",
    "INTERRUPTED",
    "SKIPPED",
  ];

  return values.includes(value as RunStatus) ? (value as RunStatus) : undefined;
}

/** 只接受合同允许的发布后健康筛选。 */
function parseHealthStatus(value: string | null): HealthStatus | undefined {
  const values: HealthStatus[] = ["HEALTHY", "WARN", "CRITICAL", "UNKNOWN"];
  return values.includes(value as HealthStatus) ? (value as HealthStatus) : undefined;
}

/** 读取长度受限的可分享字符串，避免将异常 URL 原样送入后端。 */
function parseBoundedString(value: string | null, maximumLength = 160): string | undefined {
  return value !== null && value.length > 0 && value.length <= maximumLength ? value : undefined;
}

/** 从 URL 解析经白名单约束的筛选和详情标识。 */
export function readDataOperationsUrlState(search: URLSearchParams): DataOperationsUrlState {
  const query = parseBoundedString(search.get("q"), 120);
  const provider = parseBoundedString(search.get("provider"), 80);
  const upstream = parseBoundedString(search.get("upstream"), 120);
  // 兼容曾经共享的目录 cursor 链接，但新链接必须为每个资源使用独立键。
  const catalogCursor =
    parseBoundedString(search.get("datasetCursor"), 1024) ??
    parseBoundedString(search.get("cursor"), 1024);
  const runCursor = parseBoundedString(search.get("runCursor"), 1024);
  const healthCursor = parseBoundedString(search.get("healthCursor"), 1024);
  const scheduleCursor = parseBoundedString(search.get("scheduleCursor"), 1024);
  const operationCursor = parseBoundedString(search.get("operationCursor"), 1024);
  const runStatus = parseRunStatus(search.get("runStatus"));
  const health = parseHealthStatus(search.get("health"));

  return {
    ...defaultState,
    tab: parseTab(search.get("tab")),
    catalog: {
      limit: 50,
      ...(query === undefined ? {} : { query }),
      ...(provider === undefined ? {} : { providers: [provider] }),
      ...(upstream === undefined ? {} : { upstreamSources: [upstream] }),
      ...(runStatus === undefined ? {} : { runStatuses: [runStatus] }),
      ...(health === undefined ? {} : { healthStatuses: [health] }),
      ...(catalogCursor === undefined ? {} : { cursor: catalogCursor }),
    },
    ...(runCursor === undefined ? {} : { runCursor }),
    ...(healthCursor === undefined ? {} : { healthCursor }),
    ...(scheduleCursor === undefined ? {} : { scheduleCursor }),
    ...(operationCursor === undefined ? {} : { operationCursor }),
    ...(runStatus === undefined ? {} : { runStatus }),
    ...(health === undefined ? {} : { health }),
    ...(parseBoundedString(search.get("dataset")) === undefined
      ? {}
      : { datasetCode: parseBoundedString(search.get("dataset")) }),
    ...(parseBoundedString(search.get("run")) === undefined
      ? {}
      : { runId: parseBoundedString(search.get("run")) }),
    ...(parseBoundedString(search.get("command")) === undefined
      ? {}
      : { commandId: parseBoundedString(search.get("command")) }),
    ...(parseBoundedString(search.get("healthCheck")) === undefined
      ? {}
      : { healthCheckId: parseBoundedString(search.get("healthCheck")) }),
    ...(parseBoundedString(search.get("evaluation")) === undefined
      ? {}
      : { evaluationId: parseBoundedString(search.get("evaluation")) }),
    ...(parseBoundedString(search.get("schedule")) === undefined
      ? {}
      : { scheduleId: parseBoundedString(search.get("schedule")) }),
    ...(parseBoundedString(search.get("submission")) === undefined
      ? {}
      : { submissionId: parseBoundedString(search.get("submission")) }),
  };
}

/** 将已验证页面状态写回 URL，不把表单草稿或敏感数据放入浏览器历史。 */
export function writeDataOperationsUrlState(state: DataOperationsUrlState): URLSearchParams {
  const search = new URLSearchParams();
  if (state.tab !== "datasets") search.set("tab", state.tab);
  if (state.catalog.query !== undefined && state.catalog.query !== null) {
    search.set("q", state.catalog.query);
  }
  if (state.catalog.providers?.[0] !== undefined)
    search.set("provider", state.catalog.providers[0]);
  if (state.catalog.upstreamSources?.[0] !== undefined) {
    search.set("upstream", state.catalog.upstreamSources[0]);
  }
  if (state.catalog.cursor !== undefined && state.catalog.cursor !== null) {
    search.set("datasetCursor", state.catalog.cursor);
  }
  if (state.runCursor !== undefined) search.set("runCursor", state.runCursor);
  if (state.healthCursor !== undefined) search.set("healthCursor", state.healthCursor);
  if (state.scheduleCursor !== undefined) search.set("scheduleCursor", state.scheduleCursor);
  if (state.operationCursor !== undefined) search.set("operationCursor", state.operationCursor);
  if (state.runStatus !== undefined) search.set("runStatus", state.runStatus);
  if (state.health !== undefined) search.set("health", state.health);
  if (state.datasetCode !== undefined) search.set("dataset", state.datasetCode);
  if (state.runId !== undefined) search.set("run", state.runId);
  if (state.commandId !== undefined) search.set("command", state.commandId);
  if (state.healthCheckId !== undefined) search.set("healthCheck", state.healthCheckId);
  if (state.evaluationId !== undefined) search.set("evaluation", state.evaluationId);
  if (state.scheduleId !== undefined) search.set("schedule", state.scheduleId);
  if (state.submissionId !== undefined) search.set("submission", state.submissionId);

  return search;
}
