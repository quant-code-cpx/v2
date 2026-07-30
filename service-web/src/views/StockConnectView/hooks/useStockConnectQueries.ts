import { queryOptions, useQuery } from "@tanstack/react-query";
import type { QueryKey } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  queryStockConnectActiveSecurities,
  queryStockConnectChannel,
  queryStockConnectOverview,
  queryStockConnectReadiness,
  queryStockConnectSecurityContext,
} from "../../../api/stock-connect";
import { queryClient } from "../../../api/query-client";
import { isApiError } from "../../../api/http";
import { useAuth } from "../../../components/AuthProvider";
import type {
  StockConnectActiveSecurityPage,
  StockConnectActiveSecurityQuery,
  StockConnectChannelCode,
  StockConnectChannelQuery,
  StockConnectChannelResponse,
  StockConnectOverviewQuery,
  StockConnectOverviewResponse,
  StockConnectReadinessQuery,
  StockConnectReadinessResponse,
  StockConnectSecurityContextQuery,
  StockConnectSecurityContextResponse,
  VersionedStockConnectResponse,
} from "../../../types/stock-connect";
import {
  stockConnectChannelCodeBySlug,
  stockConnectChannelsForDirection,
  stockConnectRankingBySlug,
  toStockConnectDateSelection,
} from "../utils/stock-connect-url";
import type {
  StockConnectSecurityUrlState,
  StockConnectUrlState,
} from "../utils/stock-connect-url";

/** 统一构造按用户、业务资源和全部请求筛选隔离的查询键。 */
export const stockConnectKeys = {
  /** 构造共同交易日总览查询键。 */
  overview: (actorRef: string, request: StockConnectOverviewQuery) =>
    ["stock-connect", "overview", actorRef, request] as const,
  /** 构造独立候选交易日 readiness 查询键。 */
  readiness: (actorRef: string, request: StockConnectReadinessQuery) =>
    ["stock-connect", "readiness", actorRef, request] as const,
  /** 构造单通道详情查询键。 */
  channel: (actorRef: string, request: StockConnectChannelQuery) =>
    ["stock-connect", "channel", actorRef, request] as const,
  /** 构造绑定父 publication 的活跃证券查询键。 */
  active: (actorRef: string, collectionVersion: string, request: StockConnectActiveSecurityQuery) =>
    ["stock-connect", "active", actorRef, collectionVersion, request] as const,
  /** 构造证券互联互通上下文查询键。 */
  security: (actorRef: string, request: StockConnectSecurityContextQuery) =>
    ["stock-connect", "security", actorRef, request] as const,
};

/** 只重试一次网络、限流或服务依赖故障，不重试确定的日期、权限和校验结果。 */
function shouldRetryStockConnect(failureCount: number, error: unknown): boolean {
  if (failureCount >= 1) {
    return false;
  }
  if (!isApiError(error)) {
    return true;
  }

  return error.status === 429 || error.status === 502 || error.status === 503;
}

/** 返回沪深港通日终 publication 查询的统一缓存与复核策略。 */
function stockConnectQueryPolicy() {
  return {
    staleTime: 5 * 60 * 1_000,
    gcTime: 30 * 60 * 1_000,
    refetchOnWindowFocus: true,
    retry: shouldRetryStockConnect,
  } as const;
}

/** 构造总览 Query，并通过缓存版本发送 If-None-Match。 */
function overviewQueryOptions(actorRef: string, request: StockConnectOverviewQuery) {
  const queryKey = stockConnectKeys.overview(actorRef, request);

  /** 复核当前总览 publication，204 时复用完全相同查询键的数据。 */
  const queryFn = ({ signal }: { signal: AbortSignal }) =>
    queryStockConnectOverview(request, {
      previous:
        queryClient.getQueryData<VersionedStockConnectResponse<StockConnectOverviewResponse>>(
          queryKey,
        ),
      signal,
    });

  return queryOptions({
    queryKey,
    queryFn,
    ...stockConnectQueryPolicy(),
  });
}

/** 构造独立 readiness Query，并用其 SHA-256 表示版本执行条件复核。 */
function readinessQueryOptions(actorRef: string, request: StockConnectReadinessQuery) {
  const queryKey = stockConnectKeys.readiness(actorRef, request);

  /** 复核当前候选日证据快照，204 时复用同一用户和筛选范围的实体。 */
  const queryFn = ({ signal }: { signal: AbortSignal }) =>
    queryStockConnectReadiness(request, {
      previous:
        queryClient.getQueryData<VersionedStockConnectResponse<StockConnectReadinessResponse>>(
          queryKey,
        ),
      signal,
    });

  return queryOptions({
    queryKey,
    queryFn,
    ...stockConnectQueryPolicy(),
    staleTime: 60 * 1_000,
  });
}

/** 构造通道详情 Query，并通过缓存版本发送 If-None-Match。 */
function channelQueryOptions(actorRef: string, request: StockConnectChannelQuery) {
  const queryKey = stockConnectKeys.channel(actorRef, request);

  /** 复核当前单通道 publication，204 时复用同键详情。 */
  const queryFn = ({ signal }: { signal: AbortSignal }) =>
    queryStockConnectChannel(request, {
      previous:
        queryClient.getQueryData<VersionedStockConnectResponse<StockConnectChannelResponse>>(
          queryKey,
        ),
      signal,
    });

  return queryOptions({
    queryKey,
    queryFn,
    ...stockConnectQueryPolicy(),
  });
}

/** 构造绑定父 publication 的活跃证券 Query。 */
function activeSecurityQueryOptions(
  actorRef: string,
  collectionVersion: string,
  request: StockConnectActiveSecurityQuery,
) {
  const queryKey = stockConnectKeys.active(actorRef, collectionVersion, request);

  /** 复核当前来源活跃榜页，禁止把其他 publication 的游标页带入。 */
  const queryFn = ({ signal }: { signal: AbortSignal }) =>
    queryStockConnectActiveSecurities(request, {
      previous:
        queryClient.getQueryData<VersionedStockConnectResponse<StockConnectActiveSecurityPage>>(
          queryKey,
        ),
      signal,
    });

  return queryOptions({
    queryKey,
    queryFn,
    ...stockConnectQueryPolicy(),
  });
}

/** 构造证券互联互通上下文 Query。 */
function securityContextQueryOptions(actorRef: string, request: StockConnectSecurityContextQuery) {
  const queryKey = stockConnectKeys.security(actorRef, request);

  /** 复核当前证券上下文 publication，204 时保持已验证实体。 */
  const queryFn = ({ signal }: { signal: AbortSignal }) =>
    queryStockConnectSecurityContext(request, {
      previous:
        queryClient.getQueryData<
          VersionedStockConnectResponse<StockConnectSecurityContextResponse>
        >(queryKey),
      signal,
    });

  return queryOptions({
    queryKey,
    queryFn,
    ...stockConnectQueryPolicy(),
  });
}

/** 表示父 publication 与活跃榜原子关联的自动复核阶段。 */
export type StockConnectParentPublicationRecoveryStatus = "idle" | "recovering" | "exhausted";

/** 保存一个业务筛选范围内至多一次自动复核的执行状态。 */
interface ParentPublicationRecoveryGuard {
  scopeKey: string;
  inFlight: boolean;
  completed: boolean;
}

/** 在父 publication 漂移时整体失效父查询和活跃榜，避免跨版本拼接。 */
export function useStockConnectParentPublicationRecovery({
  activeError,
  activeSucceeded,
  parentQueryKey,
  activeQueryKey,
  scopeKey,
}: {
  activeError: unknown;
  activeSucceeded: boolean;
  parentQueryKey: QueryKey;
  activeQueryKey: QueryKey;
  scopeKey: string;
}): {
  status: StockConnectParentPublicationRecoveryStatus;
  retryPublicationPair: () => void;
} {
  const recoveryGuard = useRef<ParentPublicationRecoveryGuard | null>(null);
  const [phase, setPhase] = useState<StockConnectParentPublicationRecoveryStatus | "retried">(
    "idle",
  );

  /** 启动一次父查询优先、活跃榜随后执行的整体 publication 复核。 */
  const runRecovery = useCallback(() => {
    const currentGuard = recoveryGuard.current;
    if (currentGuard?.scopeKey === scopeKey && currentGuard.inFlight) {
      return;
    }

    const guard: ParentPublicationRecoveryGuard = {
      scopeKey,
      inFlight: true,
      completed: false,
    };
    recoveryGuard.current = guard;
    setPhase("recovering");

    /** 先刷新父 publication，再让仍处于观察状态的同版本活跃榜重新请求。 */
    const recover = async () => {
      try {
        await queryClient.invalidateQueries({
          queryKey: parentQueryKey,
          exact: true,
          refetchType: "active",
        });
        await queryClient.invalidateQueries({
          queryKey: activeQueryKey,
          exact: true,
          refetchType: "active",
        });
      } finally {
        if (recoveryGuard.current === guard) {
          guard.inFlight = false;
          guard.completed = true;
          setPhase("retried");
        }
      }
    };

    void recover();
  }, [activeQueryKey, parentQueryKey, scopeKey]);

  /** 观察 PARENT_PUBLICATION_MISMATCH，并确保每个业务筛选范围最多自动复核一次。 */
  useEffect(() => {
    const currentGuard = recoveryGuard.current;
    if (currentGuard !== null && currentGuard.scopeKey !== scopeKey) {
      recoveryGuard.current = null;
      setPhase("idle");
    }
    if (activeSucceeded) {
      recoveryGuard.current = null;
      setPhase("idle");
      return;
    }

    const isParentMismatch =
      isApiError(activeError) && activeError.code === "PARENT_PUBLICATION_MISMATCH";
    if (!isParentMismatch) {
      return;
    }

    const scopedGuard = recoveryGuard.current?.scopeKey === scopeKey ? recoveryGuard.current : null;
    if (scopedGuard === null) {
      runRecovery();
      return;
    }
    if (!scopedGuard.inFlight && scopedGuard.completed) {
      setPhase("exhausted");
    }
  }, [activeError, activeSucceeded, phase, runRecovery, scopeKey]);

  /** 用户手动重试时仍整体刷新父查询与榜单，不允许只重试失配子查询。 */
  const retryPublicationPair = useCallback(() => {
    if (recoveryGuard.current?.inFlight === true) {
      return;
    }
    recoveryGuard.current = null;
    runRecovery();
  }, [runRecovery]);

  return {
    status: phase === "retried" ? "recovering" : phase,
    retryPublicationPair,
  };
}

/** 编排总览、当前趋势通道和同一共同交易日来源活跃榜。 */
export function useStockConnectOverviewQueries(state: StockConnectUrlState) {
  const { user } = useAuth();
  const actorRef = user?.id ?? "session-pending";

  /** 构造包含共同日期、方向通道集和趋势窗口的总览请求。 */
  const overviewRequest = useMemo<StockConnectOverviewQuery>(
    () => ({
      date: toStockConnectDateSelection(state.date),
      channels: stockConnectChannelsForDirection(state.direction),
      trendTradingDays: state.trendDays,
    }),
    [state.date, state.direction, state.trendDays],
  );
  const overviewQuery = useQuery({
    ...overviewQueryOptions(actorRef, overviewRequest),
    enabled: user !== undefined,
  });
  const readinessRequest = useMemo<StockConnectReadinessQuery>(
    () => ({
      date: overviewRequest.date,
      channels: overviewRequest.channels,
    }),
    [overviewRequest],
  );
  const readinessQuery = useQuery({
    ...readinessQueryOptions(actorRef, readinessRequest),
    enabled: user !== undefined,
  });
  const overviewQueryKey = useMemo(
    () => stockConnectKeys.overview(actorRef, overviewRequest),
    [actorRef, overviewRequest],
  );
  const selectedChannel = stockConnectChannelCodeBySlug[
    state.channel
  ] satisfies StockConnectChannelCode;
  const overview = overviewQuery.data?.data;
  const selectedChannelExists =
    overview?.channels.some(
      /** 检查选中通道确实属于本次共同交易日 bundle。 */
      (item) => item.channel === selectedChannel,
    ) ?? false;
  const collectionVersion = overview?.publication.dataVersion ?? "publication-pending";

  /** 把来源榜锁定到总览实际解析交易日和同一父 publication。 */
  const activeRequest = useMemo<StockConnectActiveSecurityQuery>(
    () => ({
      date:
        overview === undefined
          ? toStockConnectDateSelection(state.date)
          : { mode: "EXACT", exactDate: overview.resolvedTradeDate },
      channel: selectedChannel,
      ranking: stockConnectRankingBySlug[state.ranking],
      parentPublicationDataVersion: collectionVersion,
      cursor: state.cursor ?? null,
      limit: state.pageSize,
    }),
    [
      collectionVersion,
      overview,
      selectedChannel,
      state.cursor,
      state.date,
      state.pageSize,
      state.ranking,
    ],
  );
  const activeQueryKey = useMemo(
    () => stockConnectKeys.active(actorRef, collectionVersion, activeRequest),
    [actorRef, activeRequest, collectionVersion],
  );
  const activeQuery = useQuery({
    ...activeSecurityQueryOptions(actorRef, collectionVersion, activeRequest),
    enabled: user !== undefined && overview !== undefined && selectedChannelExists,
  });
  const recoveryScopeKey = useMemo(
    () =>
      JSON.stringify([
        "overview",
        actorRef,
        overviewRequest,
        selectedChannel,
        state.ranking,
        state.cursor ?? null,
        state.pageSize,
      ]),
    [actorRef, overviewRequest, selectedChannel, state.cursor, state.pageSize, state.ranking],
  );
  const parentPublicationRecovery = useStockConnectParentPublicationRecovery({
    activeError: activeQuery.error,
    activeSucceeded: activeQuery.isSuccess,
    parentQueryKey: overviewQueryKey,
    activeQueryKey,
    scopeKey: recoveryScopeKey,
  });

  return {
    overviewQuery,
    readinessQuery,
    activeQuery,
    selectedChannel,
    parentPublicationRecovery,
  };
}

/** 编排单通道详情与严格绑定其解析交易日和 publication 的来源榜。 */
export function useStockConnectChannelQueries(
  channel: StockConnectChannelCode,
  state: StockConnectUrlState,
) {
  const { user } = useAuth();
  const actorRef = user?.id ?? "session-pending";

  /** 构造由 path 通道持有的详情请求。 */
  const channelRequest = useMemo<StockConnectChannelQuery>(
    () => ({
      date: toStockConnectDateSelection(state.date),
      channel,
      trendTradingDays: state.trendDays,
    }),
    [channel, state.date, state.trendDays],
  );
  const channelQuery = useQuery({
    ...channelQueryOptions(actorRef, channelRequest),
    enabled: user !== undefined,
  });
  const readinessRequest = useMemo<StockConnectReadinessQuery>(
    () => ({
      date: channelRequest.date,
      channels: [channel],
    }),
    [channel, channelRequest.date],
  );
  const readinessQuery = useQuery({
    ...readinessQueryOptions(actorRef, readinessRequest),
    enabled: user !== undefined,
  });
  const channelQueryKey = useMemo(
    () => stockConnectKeys.channel(actorRef, channelRequest),
    [actorRef, channelRequest],
  );
  const channelResponse = channelQuery.data?.data;
  const collectionVersion = channelResponse?.publication.dataVersion ?? "publication-pending";

  /** 把详情榜单锁定到通道实际解析交易日和同一父 publication。 */
  const activeRequest = useMemo<StockConnectActiveSecurityQuery>(
    () => ({
      date:
        channelResponse === undefined
          ? toStockConnectDateSelection(state.date)
          : { mode: "EXACT", exactDate: channelResponse.resolvedTradeDate },
      channel,
      ranking: stockConnectRankingBySlug[state.ranking],
      parentPublicationDataVersion: collectionVersion,
      cursor: state.cursor ?? null,
      limit: state.pageSize,
    }),
    [
      channel,
      channelResponse,
      collectionVersion,
      state.cursor,
      state.date,
      state.pageSize,
      state.ranking,
    ],
  );
  const activeQueryKey = useMemo(
    () => stockConnectKeys.active(actorRef, collectionVersion, activeRequest),
    [actorRef, activeRequest, collectionVersion],
  );
  const activeQuery = useQuery({
    ...activeSecurityQueryOptions(actorRef, collectionVersion, activeRequest),
    enabled: user !== undefined && channelResponse !== undefined,
  });
  const recoveryScopeKey = useMemo(
    () =>
      JSON.stringify([
        "channel",
        actorRef,
        channelRequest,
        state.ranking,
        state.cursor ?? null,
        state.pageSize,
      ]),
    [actorRef, channelRequest, state.cursor, state.pageSize, state.ranking],
  );
  const parentPublicationRecovery = useStockConnectParentPublicationRecovery({
    activeError: activeQuery.error,
    activeSucceeded: activeQuery.isSuccess,
    parentQueryKey: channelQueryKey,
    activeQueryKey,
    scopeKey: recoveryScopeKey,
  });

  return { channelQuery, readinessQuery, activeQuery, parentPublicationRecovery };
}

/** 查询稳定证券身份的互联互通上下文，不请求完整港股行情。 */
export function useStockConnectSecurityQuery(
  instrumentEntityRef: string,
  state: StockConnectSecurityUrlState,
) {
  const { user } = useAuth();
  const actorRef = user?.id ?? "session-pending";

  /** 构造稳定实体引用、可选通道和历史交易日窗口请求。 */
  const request = useMemo<StockConnectSecurityContextQuery>(
    () => ({
      instrumentEntityRef,
      date: toStockConnectDateSelection(state.date),
      channel: state.channel === undefined ? null : stockConnectChannelCodeBySlug[state.channel],
      historyTradingDays: state.trendDays,
    }),
    [instrumentEntityRef, state.channel, state.date, state.trendDays],
  );

  return useQuery({
    ...securityContextQueryOptions(actorRef, request),
    enabled: user !== undefined && instrumentEntityRef.length > 0,
  });
}
