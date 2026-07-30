import { keepPreviousData, queryOptions } from "@tanstack/react-query";

import { authSession } from "./auth-session";
import { requestJson } from "./http";
import type {
  CommandActionRequest,
  CommandDetailView,
  DatasetDetail,
  DatasetPage,
  DatasetSearchRequest,
  HealthCheckDetailView,
  HealthCheckSubmitRequest,
  HealthDetail,
  HealthDetailRequest,
  HealthPage,
  HealthSearchRequest,
  OperationPage,
  OperationSearchRequest,
  OperationsOverview,
  RunDetail,
  RunDetailRequest,
  RunPage,
  RunSearchRequest,
  ScheduleEnabledRequest,
  SchedulePage,
  ScheduleSearchRequest,
  ScheduleUpsertRequest,
  SubmissionReceipt,
  SyncPreflight,
  SyncPreflightRequest,
  SyncSubmitRequest,
} from "../types/data-operations";

/** 数据运维公开合同的固定 URL 前缀。 */
const dataOperationsApiPrefix = "/api/v1/data-operations";

/** 写操作必须稳定携带的公开幂等与关联标识。 */
export interface DataOperationWriteOptions {
  idempotencyKey: string;
  requestId?: string;
}

/** 为一次新的浏览器用户意图生成可跨网络重试复用的公开幂等键。 */
export function createDataOperationIdempotencyKey(): string {
  return `dataops-${globalThis.crypto.randomUUID()}`;
}

/** 为一条浏览器到 API 的数据运维调用生成关联标识。 */
export function createDataOperationRequestId(): string {
  return globalThis.crypto.randomUUID();
}

/** 使用内存 Bearer access token 调用唯一允许的数据运维公开边界。 */
async function requestDataOperations<T>(
  path: string,
  body: unknown,
  options?: Partial<DataOperationWriteOptions>,
): Promise<T> {
  return authSession.withAccessToken(async (accessToken) => {
    const headers: Record<string, string> = {
      Authorization: `Bearer ${accessToken}`,
    };

    if (options?.idempotencyKey !== undefined) {
      headers["Idempotency-Key"] = options.idempotencyKey;
    }
    if (options?.requestId !== undefined) {
      headers["X-Request-Id"] = options.requestId;
    }

    return requestJson<T>(`${dataOperationsApiPrefix}${path}`, { headers, body });
  });
}

/** 查询全局执行槽、目录规模、队列与本地投递状态。 */
export function getDataOperationsOverview(): Promise<OperationsOverview> {
  return requestDataOperations<OperationsOverview>("/overview", {});
}

/** 按公开筛选条件检索数据资产目录。 */
export function searchOperationalDatasets(input: DatasetSearchRequest): Promise<DatasetPage> {
  return requestDataOperations<DatasetPage>("/datasets/search", input);
}

/** 读取一个数据集的来源、时间、健康与计划详情。 */
export function getOperationalDataset(datasetCode: string): Promise<DatasetDetail> {
  return requestDataOperations<DatasetDetail>("/datasets/detail", { datasetCode });
}

/** 对单个或批量同步目标执行无副作用预检。 */
export function preflightDataSync(input: SyncPreflightRequest): Promise<SyncPreflight> {
  return requestDataOperations<SyncPreflight>("/sync/preflight", input);
}

/** 持久化同步意图，初始回执只能表示本地 PENDING。 */
export function submitDataSync(
  input: SyncSubmitRequest,
  options: DataOperationWriteOptions,
): Promise<SubmissionReceipt> {
  return requestDataOperations<SubmissionReceipt>("/sync/submit", input, {
    ...options,
    requestId: options.requestId ?? createDataOperationRequestId(),
  });
}

/** 持久化对命令或运行的合作式取消意图。 */
export function cancelDataSync(
  input: CommandActionRequest,
  options: DataOperationWriteOptions,
): Promise<SubmissionReceipt> {
  return requestDataOperations<SubmissionReceipt>("/sync/cancel", input, {
    ...options,
    requestId: options.requestId ?? createDataOperationRequestId(),
  });
}

/** 持久化对失败命令或运行的重试意图。 */
export function retryDataSync(
  input: CommandActionRequest,
  options: DataOperationWriteOptions,
): Promise<SubmissionReceipt> {
  return requestDataOperations<SubmissionReceipt>("/sync/retry", input, {
    ...options,
    requestId: options.requestId ?? createDataOperationRequestId(),
  });
}

/** 读取命令及按原提交顺序返回的 child runs。 */
export function getDataSyncCommand(commandId: string): Promise<CommandDetailView> {
  return requestDataOperations<CommandDetailView>("/commands/detail", { commandId });
}

/** 搜索全局串行队列和历史运行。 */
export function searchDataSyncRuns(input: RunSearchRequest): Promise<RunPage> {
  return requestDataOperations<RunPage>("/runs/search", input);
}

/** 读取运行的公开详情及独立 cursor 分区和时间线页。 */
export function getDataSyncRun(input: RunDetailRequest): Promise<RunDetail> {
  return requestDataOperations<RunDetail>("/runs/detail", input);
}

/** 搜索不可变发布后健康评估摘要。 */
export function searchDatasetHealthEvaluations(input: HealthSearchRequest): Promise<HealthPage> {
  return requestDataOperations<HealthPage>("/health/evaluations/search", input);
}

/** 读取不可变评估事实与当前开放问题投影。 */
export function getDatasetHealthEvaluation(input: HealthDetailRequest): Promise<HealthDetail> {
  return requestDataOperations<HealthDetail>("/health/evaluations/detail", input);
}

/** 持久化主动健康检查意图。 */
export function submitDatasetHealthCheck(
  input: HealthCheckSubmitRequest,
  options: DataOperationWriteOptions,
): Promise<SubmissionReceipt> {
  return requestDataOperations<SubmissionReceipt>("/health/checks/submit", input, {
    ...options,
    requestId: options.requestId ?? createDataOperationRequestId(),
  });
}

/** 读取按原 target 顺序返回的主动健康检查批次。 */
export function getDatasetHealthCheck(healthCheckId: string): Promise<HealthCheckDetailView> {
  return requestDataOperations<HealthCheckDetailView>("/health/checks/detail", { healthCheckId });
}

/** 搜索数据集自动同步计划。 */
export function searchDataSyncSchedules(input: ScheduleSearchRequest): Promise<SchedulePage> {
  return requestDataOperations<SchedulePage>("/schedules/search", input);
}

/** 持久化创建或乐观锁更新计划的意图。 */
export function upsertDataSyncSchedule(
  input: ScheduleUpsertRequest,
  options: DataOperationWriteOptions,
): Promise<SubmissionReceipt> {
  return requestDataOperations<SubmissionReceipt>("/schedules/upsert", input, {
    ...options,
    requestId: options.requestId ?? createDataOperationRequestId(),
  });
}

/** 持久化启停计划的意图。 */
export function setDataSyncScheduleEnabled(
  input: ScheduleEnabledRequest,
  options: DataOperationWriteOptions,
): Promise<SubmissionReceipt> {
  return requestDataOperations<SubmissionReceipt>("/schedules/set-enabled", input, {
    ...options,
    requestId: options.requestId ?? createDataOperationRequestId(),
  });
}

/** 对账一个 API submission 的投递与权威资源状态。 */
export function getDataOperationSubmission(submissionId: string): Promise<SubmissionReceipt> {
  return requestDataOperations<SubmissionReceipt>("/submissions/detail", { submissionId });
}

/** 搜索用户与系统来源的公开操作记录。 */
export function searchDataOperations(input: OperationSearchRequest): Promise<OperationPage> {
  return requestDataOperations<OperationPage>("/operations/search", input);
}

/** 构造总览查询，后台页面不持续轮询。 */
export function dataOperationsOverviewQueryOptions() {
  return queryOptions({
    queryKey: ["dataOperations", "summary"] as const,
    queryFn: getDataOperationsOverview,
    staleTime: 15_000,
  });
}

/** 构造与可分享筛选绑定的数据资产目录查询。 */
export function operationalDatasetSearchQueryOptions(input: DatasetSearchRequest) {
  return queryOptions({
    queryKey: ["dataOperations", "catalog", input] as const,
    queryFn: () => searchOperationalDatasets(input),
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  });
}

/** 构造按数据集按需加载的详情查询。 */
export function operationalDatasetDetailQueryOptions(datasetCode: string) {
  return queryOptions({
    queryKey: ["dataOperations", "dataset", datasetCode] as const,
    queryFn: () => getOperationalDataset(datasetCode),
    staleTime: 60_000,
  });
}

/** 构造全局队列和运行历史查询。 */
export function dataSyncRunSearchQueryOptions(input: RunSearchRequest) {
  return queryOptions({
    queryKey: ["dataOperations", "runs", input] as const,
    queryFn: () => searchDataSyncRuns(input),
    placeholderData: keepPreviousData,
    staleTime: 15_000,
  });
}

/** 构造命令详情查询，child run 顺序完全由服务端提供。 */
export function dataSyncCommandQueryOptions(commandId: string) {
  return queryOptions({
    queryKey: ["dataOperations", "command", commandId] as const,
    queryFn: () => getDataSyncCommand(commandId),
    staleTime: 5_000,
  });
}

/** 构造带独立 partition 与 timeline cursor 的运行详情查询。 */
export function dataSyncRunDetailQueryOptions(input: RunDetailRequest) {
  return queryOptions({
    queryKey: ["dataOperations", "run", input] as const,
    queryFn: () => getDataSyncRun(input),
    staleTime: 5_000,
  });
}

/** 构造发布后健康评估列表查询。 */
export function dataHealthSearchQueryOptions(input: HealthSearchRequest) {
  return queryOptions({
    queryKey: ["dataOperations", "health", input] as const,
    queryFn: () => searchDatasetHealthEvaluations(input),
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  });
}

/** 构造带独立问题 cursor 的健康详情查询。 */
export function dataHealthDetailQueryOptions(input: HealthDetailRequest) {
  return queryOptions({
    queryKey: ["dataOperations", "healthEvaluation", input] as const,
    queryFn: () => getDatasetHealthEvaluation(input),
    staleTime: 30_000,
  });
}

/** 构造主动健康检查批次查询。 */
export function dataHealthCheckQueryOptions(healthCheckId: string) {
  return queryOptions({
    queryKey: ["dataOperations", "healthCheck", healthCheckId] as const,
    queryFn: () => getDatasetHealthCheck(healthCheckId),
    staleTime: 5_000,
  });
}

/** 构造自动计划列表查询。 */
export function dataSyncScheduleSearchQueryOptions(input: ScheduleSearchRequest) {
  return queryOptions({
    queryKey: ["dataOperations", "schedules", input] as const,
    queryFn: () => searchDataSyncSchedules(input),
    placeholderData: keepPreviousData,
    staleTime: 300_000,
  });
}

/** 构造 API submission 对账查询。 */
export function dataOperationSubmissionQueryOptions(submissionId: string) {
  return queryOptions({
    queryKey: ["dataOperations", "submission", submissionId] as const,
    queryFn: () => getDataOperationSubmission(submissionId),
    staleTime: 0,
  });
}

/** 构造公开操作记录查询。 */
export function dataOperationSearchQueryOptions(input: OperationSearchRequest) {
  return queryOptions({
    queryKey: ["dataOperations", "operations", input] as const,
    queryFn: () => searchDataOperations(input),
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
}
