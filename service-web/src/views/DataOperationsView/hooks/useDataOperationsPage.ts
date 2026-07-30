import { useQuery } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import {
  dataHealthSearchQueryOptions,
  dataOperationSearchQueryOptions,
  dataOperationsOverviewQueryOptions,
  dataSyncRunSearchQueryOptions,
  dataSyncScheduleSearchQueryOptions,
  operationalDatasetSearchQueryOptions,
} from "../../../api/data-operations";
import { useAuth } from "../../../components/AuthProvider";
import type { DatasetSearchRequest, HealthStatus, RunStatus } from "../../../types/data-operations";
import { isRunTerminal } from "../utils/data-operations-presentation";
import {
  readDataOperationsUrlState,
  writeDataOperationsUrlState,
} from "../utils/data-operations-url";
import type { DataOperationsTab } from "../utils/data-operations-presentation";
import type { DataOperationsUrlState } from "../utils/data-operations-url";

/** 读取运行列表后决定全局队列轮询间隔。 */
function runPollingInterval(statuses: RunStatus[] | undefined): number {
  return statuses?.some((status) => !isRunTerminal(status)) === true ? 2_000 : 15_000;
}

/** 管理 URL、页面级并行查询、角色能力与详情导航。 */
export function useDataOperationsPage() {
  const { user } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const state = useMemo(() => readDataOperationsUrlState(searchParams), [searchParams]);
  /** 只有合同允许的管理角色可读取运维控制面。 */
  const canRead = user?.role === "ADMIN" || user?.role === "SUPER_ADMIN";
  /** 只有超级管理员可提交主动运维意图，服务端仍必须再次授权。 */
  const canWrite = user?.role === "SUPER_ADMIN";

  const overviewQuery = useQuery({
    ...dataOperationsOverviewQueryOptions(),
    enabled: canRead,
    /** 运行或恢复期间加快刷新，空闲时仅低频刷新。 */
    refetchInterval: (query) =>
      query.state.data?.dataSync.executionSlot.state === "RUNNING" ||
      query.state.data?.dataSync.executionSlot.state === "RECOVERING"
        ? 2_000
        : 15_000,
  });
  const catalogQuery = useQuery({
    ...operationalDatasetSearchQueryOptions(state.catalog),
    enabled: canRead,
  });
  const runsQuery = useQuery({
    ...dataSyncRunSearchQueryOptions({
      cursor: state.runCursor,
      limit: 20,
      ...(state.runStatus === undefined ? {} : { statuses: [state.runStatus] }),
    }),
    enabled: canRead,
    /** 只有可见非终态运行时才以两秒频率观察队列。 */
    refetchInterval: (query) =>
      runPollingInterval(query.state.data?.items.map((item) => item.status)),
  });
  const healthQuery = useQuery({
    ...dataHealthSearchQueryOptions({
      cursor: state.healthCursor,
      limit: 20,
      ...(state.health === undefined ? {} : { statuses: [state.health] }),
    }),
    enabled: canRead,
  });
  const schedulesQuery = useQuery({
    ...dataSyncScheduleSearchQueryOptions({ cursor: state.scheduleCursor, limit: 20 }),
    enabled: canRead,
  });
  const operationsQuery = useQuery({
    ...dataOperationSearchQueryOptions({ cursor: state.operationCursor, limit: 20 }),
    enabled: canRead,
  });

  /** 以一个完整已验证状态替换 URL，避免保留已关闭 Drawer 标识。 */
  const replaceState = useCallback(
    (nextState: DataOperationsUrlState) => {
      setSearchParams(writeDataOperationsUrlState(nextState));
    },
    [setSearchParams],
  );

  /** 切换任务型 Tab，并保留可分享的目录筛选。 */
  const setTab = useCallback(
    (tab: DataOperationsTab) => {
      replaceState({ ...state, tab });
    },
    [replaceState, state],
  );

  /** 更新目录筛选且将 cursor 重置为首页。 */
  const updateCatalog = useCallback(
    (update: Partial<DatasetSearchRequest>) => {
      const catalog = {
        ...state.catalog,
        ...update,
        ...(update.cursor === undefined ? { cursor: undefined } : {}),
      };
      replaceState({ ...state, catalog });
    },
    [replaceState, state],
  );

  /** 仅更新运行状态筛选，并让目录与队列同步回到首页。 */
  const setRunStatus = useCallback(
    (runStatus: RunStatus | undefined) => {
      const catalog = {
        ...state.catalog,
        cursor: undefined,
        ...(runStatus === undefined ? { runStatuses: undefined } : { runStatuses: [runStatus] }),
      };
      replaceState({ ...state, catalog, runStatus, runCursor: undefined });
    },
    [replaceState, state],
  );

  /** 仅更新发布后健康筛选，并让目录与健康列表同步回到首页。 */
  const setHealthStatus = useCallback(
    (health: HealthStatus | undefined) => {
      const catalog = {
        ...state.catalog,
        cursor: undefined,
        ...(health === undefined ? { healthStatuses: undefined } : { healthStatuses: [health] }),
      };
      replaceState({ ...state, catalog, health, healthCursor: undefined });
    },
    [replaceState, state],
  );

  /** 更新同步任务自己的 cursor，绝不复用目录或健康评估的分页定位。 */
  const setRunCursor = useCallback(
    (runCursor: string | undefined) => {
      replaceState({ ...state, runCursor });
    },
    [replaceState, state],
  );

  /** 更新健康评估自己的 cursor，避免错误地向评估端点传目录 cursor。 */
  const setHealthCursor = useCallback(
    (healthCursor: string | undefined) => {
      replaceState({ ...state, healthCursor });
    },
    [replaceState, state],
  );

  /** 更新自动计划自己的 cursor，计划翻页不影响其他工作台资源。 */
  const setScheduleCursor = useCallback(
    (scheduleCursor: string | undefined) => {
      replaceState({ ...state, scheduleCursor });
    },
    [replaceState, state],
  );

  /** 更新操作记录自己的 cursor，禁止跨资源混用不透明分页令牌。 */
  const setOperationCursor = useCallback(
    (operationCursor: string | undefined) => {
      replaceState({ ...state, operationCursor });
    },
    [replaceState, state],
  );

  /** 打开指定数据集详情，同时保持当前列表上下文。 */
  const openDataset = useCallback(
    (datasetCode: string) => {
      replaceState({
        ...state,
        datasetCode,
        runId: undefined,
        commandId: undefined,
        healthCheckId: undefined,
        evaluationId: undefined,
        scheduleId: undefined,
        submissionId: undefined,
      });
    },
    [replaceState, state],
  );

  /** 打开指定 run 的公开详情。 */
  const openRun = useCallback(
    (runId: string) => {
      replaceState({
        ...state,
        datasetCode: undefined,
        runId,
        commandId: undefined,
        healthCheckId: undefined,
        evaluationId: undefined,
        scheduleId: undefined,
        submissionId: undefined,
      });
    },
    [replaceState, state],
  );

  /** 打开权威命令详情，不从分页 run 结果客户端重建批次。 */
  const openCommand = useCallback(
    (commandId: string) => {
      replaceState({
        ...state,
        datasetCode: undefined,
        runId: undefined,
        commandId,
        healthCheckId: undefined,
        evaluationId: undefined,
        scheduleId: undefined,
        submissionId: undefined,
      });
    },
    [replaceState, state],
  );

  /** 打开按原 target 顺序恢复的主动健康检查批次。 */
  const openHealthCheck = useCallback(
    (healthCheckId: string) => {
      replaceState({
        ...state,
        datasetCode: undefined,
        runId: undefined,
        commandId: undefined,
        healthCheckId,
        evaluationId: undefined,
        scheduleId: undefined,
        submissionId: undefined,
      });
    },
    [replaceState, state],
  );

  /** 打开绑定 release 的不可变健康评估。 */
  const openEvaluation = useCallback(
    (evaluationId: string) => {
      replaceState({
        ...state,
        datasetCode: undefined,
        runId: undefined,
        commandId: undefined,
        healthCheckId: undefined,
        evaluationId,
        scheduleId: undefined,
        submissionId: undefined,
      });
    },
    [replaceState, state],
  );

  /** 打开计划编辑器；创建时不写无效的空 schedule 标识。 */
  const openSchedule = useCallback(
    (scheduleId?: string) => {
      replaceState({
        ...state,
        datasetCode: undefined,
        runId: undefined,
        commandId: undefined,
        healthCheckId: undefined,
        evaluationId: undefined,
        scheduleId: scheduleId ?? "create",
        submissionId: undefined,
      });
    },
    [replaceState, state],
  );

  /** 打开写入后 submission 对账视图。 */
  const openSubmission = useCallback(
    (submissionId: string) => {
      replaceState({
        ...state,
        datasetCode: undefined,
        runId: undefined,
        commandId: undefined,
        healthCheckId: undefined,
        evaluationId: undefined,
        scheduleId: undefined,
        submissionId,
      });
    },
    [replaceState, state],
  );

  /** 关闭全部详情型 URL 标识，保留当前可分享筛选与 Tab。 */
  const closeDetails = useCallback(() => {
    replaceState({
      ...state,
      datasetCode: undefined,
      runId: undefined,
      commandId: undefined,
      healthCheckId: undefined,
      evaluationId: undefined,
      scheduleId: undefined,
      submissionId: undefined,
    });
  }, [replaceState, state]);

  /** 对所有页面级 Query 进行非破坏性手动刷新。 */
  const refresh = useCallback(() => {
    void Promise.all([
      overviewQuery.refetch(),
      catalogQuery.refetch(),
      runsQuery.refetch(),
      healthQuery.refetch(),
      schedulesQuery.refetch(),
      operationsQuery.refetch(),
    ]);
  }, [catalogQuery, healthQuery, operationsQuery, overviewQuery, runsQuery, schedulesQuery]);

  return {
    user,
    state,
    canRead,
    canWrite,
    overviewQuery,
    catalogQuery,
    runsQuery,
    healthQuery,
    schedulesQuery,
    operationsQuery,
    setTab,
    updateCatalog,
    setRunStatus,
    setHealthStatus,
    setRunCursor,
    setHealthCursor,
    setScheduleCursor,
    setOperationCursor,
    openDataset,
    openRun,
    openCommand,
    openHealthCheck,
    openEvaluation,
    openSchedule,
    openSubmission,
    closeDetails,
    refresh,
  };
}

/** 暴露页面私有组件可复用的页面模型类型。 */
export type DataOperationsPageModel = ReturnType<typeof useDataOperationsPage>;
