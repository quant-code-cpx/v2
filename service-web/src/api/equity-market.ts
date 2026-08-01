import { infiniteQueryOptions, keepPreviousData, queryOptions } from "@tanstack/react-query";
import type { InfiniteData } from "@tanstack/react-query";
import type { ZodType } from "zod";

import {
  equityBarPageSchema,
  equityCompanyProfileSchema,
  equityCorporateActionPageSchema,
  equityDataStatusResponseSchema,
  equityEventPageSchema,
  equityFinancialReportPageSchema,
  equityFinancialReportDetailSchema,
  equityFinancialMetricPageSchema,
  equityIdentityDetailSchema,
  equityIdentityPageSchema,
  equityListingStatusHistoryPageSchema,
  equityMoneyFlowDailyPageSchema,
  equitySearchResponseSchema,
  equitySectorPageSchema,
  equityValuationPageSchema,
} from "../types/equity-market";
import { authSession } from "./auth-session";
import { ApiError, isApiError, requestJsonResponse } from "./http";
import { queryClient } from "./query-client";
import type {
  EquityBarPage,
  EquityCompanyProfile,
  EquityCorporateActionPage,
  EquityDataStatusResponse,
  EquityEventFamily,
  EquityEventPage,
  EquityExchange,
  EquityFinancialReportPage,
  EquityFinancialReportDetail,
  EquityFinancialMetricPage,
  EquityIdentityDetail,
  EquityIdentityPage,
  EquityListingStatus,
  EquityListingStatusHistoryPage,
  EquityMoneyFlowDailyPage,
  EquitySearchResponse,
  EquitySearchSortField,
  EquitySectorPage,
  EquityTradingStatus,
  EquityValuationPage,
} from "../types/equity-market";

/** 描述搜索请求中的行业、概念或申万成员约束。 */
export interface EquityMembershipFilter {
  scheme: "EASTMONEY_INDUSTRY" | "EASTMONEY_CONCEPT" | "SW2021_L1" | "SW2021_L2" | "SW2021_L3";
  code: string;
}

/** 描述统一股票发现页发送的冻结请求合同。 */
export interface EquitySearchRequest {
  q?: string;
  exchanges?: EquityExchange[];
  listingStatuses?: EquityListingStatus[];
  tradingStatuses?: EquityTradingStatus[];
  memberships?: EquityMembershipFilter[];
  sort: ReadonlyArray<{
    field: EquitySearchSortField;
    direction: "ASC" | "DESC";
  }>;
  cursor?: string;
  limit: number;
  dataVersion?: string;
}

/** 描述统一证券事件查询输入。 */
export interface EquityEventSearchRequest {
  families: EquityEventFamily[];
  asOf?: string;
  start: string;
  end: string;
  knownAt?: string;
  cursor?: string;
  limit: number;
}

/** 描述详情页一次请求的数据集状态族。 */
export interface EquityDataStatusRequest {
  families?: readonly string[];
  asOf?: string;
  knownAt?: string;
}

/** 在 TanStack Query 实体中同时保存强校验正文和条件请求 ETag。 */
export interface ConditionalEntity<T> {
  body: T;
  etag?: string;
  dataVersion?: string;
}

/** K 线最多连续读取 16,000 条，覆盖 1990 年以来全部 A 股交易日并约束浏览器内存。 */
const maximumBarPages = 8;

/** 历史估值最多读取 4,000 条，覆盖公开 API 允许的 3,660 日窗口。 */
const maximumValuationPages = 4;

/** 财务报告、行项目和指标分别采用符合 A 股完整生命周期的浏览器页预算。 */
const maximumFinancialReportPages = 16;
const maximumFinancialReportDetailPages = 4;
const maximumFinancialMetricPages = 8;
const maximumCorporateActionPages = 4;

/** 从版本化公开正文读取其稳定 publication 版本。 */
function payloadDataVersion(payload: unknown): string | undefined {
  if (typeof payload !== "object" || payload === null) return undefined;
  const record = payload as Record<string, unknown>;
  if (typeof record.dataVersion === "string") return record.dataVersion;
  if (typeof record.release !== "object" || record.release === null) return undefined;
  const release = record.release as Record<string, unknown>;
  return typeof release.dataVersion === "string" ? release.dataVersion : undefined;
}

/** 判断公开正文是否为明确的无 publication 可用性信封。 */
function isUnavailableEnvelope(payload: unknown): boolean {
  return (
    typeof payload === "object" &&
    payload !== null &&
    (payload as Record<string, unknown>).availability === "UNAVAILABLE"
  );
}

/** 条件缓存只接受 RFC 9110 强实体标签。 */
function isStrongEtag(value: string): boolean {
  return /^"[\x21\x23-\x7e]+"$/.test(value);
}

/** 读取条件实体正文，供页面模型隐藏传输元数据。 */
export function conditionalBody<T>(entity: ConditionalEntity<T> | undefined): T | undefined {
  return entity?.body;
}

/** 调用已认证公开 POST，并在 204 时复用同一个 Query key 的既有实体。 */
async function requestEquityEntity<T>(
  path: string,
  body: unknown,
  schema: ZodType<T>,
  previous: ConditionalEntity<T> | undefined,
): Promise<ConditionalEntity<T>> {
  return authSession.withAccessToken(async (accessToken) => {
    const headers: Record<string, string> = {
      Authorization: `Bearer ${accessToken}`,
    };
    if (previous?.etag !== undefined) {
      headers["If-None-Match"] = previous.etag;
    }

    const response = await requestJsonResponse<unknown>(path, {
      body,
      headers,
    });

    if (response.status !== 200 && response.status !== 204) {
      throw new ApiError(502, "conditional-status-invalid");
    }

    // 公开 POST 的 204 必须同时命中 ETag 和 publication，不能把另一版本误当作缓存命中。
    if (response.status === 204) {
      if (
        response.data !== undefined ||
        previous?.etag === undefined ||
        previous.dataVersion === undefined
      ) {
        throw new ApiError(502, "conditional-cache-miss");
      }
      if (response.headers.get("ETag") !== previous.etag) {
        throw new ApiError(502, "conditional-cache-etag-mismatch");
      }
      if (response.headers.get("X-Data-Version") !== previous.dataVersion) {
        throw new ApiError(502, "conditional-cache-version-mismatch");
      }
      return previous;
    }

    if (response.data === undefined) {
      throw new ApiError(502, "missing-response-body");
    }

    const result = schema.safeParse(response.data);
    if (!result.success) {
      throw new ApiError(502, "contract-invalid");
    }
    const etag = response.headers.get("ETag");
    const dataVersion = response.headers.get("X-Data-Version");

    // 无 publication 信封没有可重验证实体；其余 200 必须完整绑定强 ETag 与数据版本。
    if (
      etag === null &&
      dataVersion === null &&
      isUnavailableEnvelope(result.data) &&
      payloadDataVersion(result.data) === undefined
    ) {
      return { body: result.data };
    }
    if (etag === null || dataVersion === null) {
      throw new ApiError(502, "conditional-headers-missing");
    }
    if (!isStrongEtag(etag)) {
      throw new ApiError(502, "conditional-etag-invalid");
    }
    const bodyDataVersion = payloadDataVersion(result.data);
    if (bodyDataVersion !== undefined && bodyDataVersion !== dataVersion) {
      throw new ApiError(502, "conditional-version-mismatch");
    }
    return { body: result.data, etag, dataVersion };
  });
}

/** 从 Query cache 读取相同 key 的条件实体，避免 transport 维护第二份事实缓存。 */
function previousEntity<T>(key: readonly unknown[]): ConditionalEntity<T> | undefined {
  return queryClient.getQueryData<ConditionalEntity<T>>(key);
}

/** 从 Infinite Query cache 中定位同一 opaque cursor 的既有条件实体。 */
function previousInfiniteEntity<T>(
  key: readonly unknown[],
  cursor: string | undefined,
): ConditionalEntity<T> | undefined {
  const cached =
    queryClient.getQueryData<InfiniteData<ConditionalEntity<T>, string | undefined>>(key);
  const pageIndex =
    cached?.pageParams.findIndex(
      /** pageParam 是服务端 opaque cursor，只允许精确相等。 */
      (pageCursor) => pageCursor === cursor,
    ) ?? -1;
  return pageIndex < 0 ? undefined : cached?.pages[pageIndex];
}

/** 构造既有 query-string POST 路由的受控参数。 */
function withSearch(path: string, values: object): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (Array.isArray(value)) {
      for (const item of value) search.append(key, String(item));
    } else if (value !== undefined) {
      search.set(key, String(value));
    }
  }
  const encoded = search.toString();
  return encoded.length === 0 ? path : `${path}?${encoded}`;
}

/** 网络和依赖故障最多重试两次；权限、限流和业务冲突交给用户动作恢复。 */
export function retryEquityQuery(failureCount: number, error: unknown): boolean {
  if (!isApiError(error)) {
    return failureCount < 2;
  }
  if ([400, 401, 403, 404, 409, 429].includes(error.status)) {
    return false;
  }
  return failureCount < 2;
}

/** 构造股票发现页 Query，响应与 ETag 始终绑定同一 key。 */
export function equitySearchQueryOptions(input: EquitySearchRequest) {
  const key = ["equities", "search", input] as const;
  return queryOptions({
    queryKey: key,
    queryFn: () =>
      requestEquityEntity(
        "/api/v1/equities/search",
        input,
        equitySearchResponseSchema,
        previousEntity<EquitySearchResponse>(key),
      ),
    placeholderData: keepPreviousData,
    staleTime: 5 * 60_000,
    gcTime: 30 * 60_000,
    retry: retryEquityQuery,
  });
}

/** 构造旧 symbol-only 路由的轻量目录解析 Query。 */
export function legacyEquityResolutionQueryOptions(symbol: string) {
  const key = ["equities", "legacy-resolution", symbol] as const;
  const path = withSearch("/api/v1/equities", { query: symbol, limit: 100 });
  return queryOptions({
    queryKey: key,
    queryFn: () =>
      requestEquityEntity(
        path,
        undefined,
        equityIdentityPageSchema,
        previousEntity<EquityIdentityPage>(key),
      ),
    staleTime: 15 * 60_000,
    gcTime: 60 * 60_000,
    retry: retryEquityQuery,
  });
}

/** 构造证券双时态身份详情 Query。 */
export function equityIdentityQueryOptions(
  exchange: EquityExchange,
  symbol: string,
  asOf?: string,
  knownAt?: string,
) {
  const key = ["equity", exchange, symbol, "identity", asOf, knownAt] as const;
  const path = withSearch(`/api/v1/equities/${exchange}/${symbol}`, {
    asOf,
    knownAt,
  });
  return queryOptions({
    queryKey: key,
    queryFn: () =>
      requestEquityEntity(
        path,
        undefined,
        equityIdentityDetailSchema,
        previousEntity<EquityIdentityDetail>(key),
      ),
    staleTime: 15 * 60_000,
    gcTime: 60 * 60_000,
    retry: retryEquityQuery,
  });
}

/** 构造证券上市生命周期历史 Query；普通停牌由独立 trading status 数据集负责。 */
export function equityListingHistoryQueryOptions(
  exchange: EquityExchange,
  symbol: string,
  asOf?: string,
) {
  const key = ["equity", exchange, symbol, "listing-status-history", asOf] as const;
  const path = withSearch(`/api/v1/equities/${exchange}/${symbol}/listing-status-history`, {
    asOf,
    limit: 100,
  });
  return queryOptions({
    queryKey: key,
    queryFn: () =>
      requestEquityEntity(
        path,
        undefined,
        equityListingStatusHistoryPageSchema,
        previousEntity<EquityListingStatusHistoryPage>(key),
      ),
    staleTime: 15 * 60_000,
    gcTime: 60 * 60_000,
    retry: retryEquityQuery,
  });
}

/** 构造详情页数据状态 Query；事实读取仍由各页签独立负责。 */
export function equityDataStatusQueryOptions(
  exchange: EquityExchange,
  symbol: string,
  input: EquityDataStatusRequest,
) {
  const key = ["equity", exchange, symbol, "data-status", input] as const;
  return queryOptions({
    queryKey: key,
    queryFn: () =>
      requestEquityEntity(
        `/api/v1/equities/${exchange}/${symbol}/data-status`,
        input,
        equityDataStatusResponseSchema,
        previousEntity<EquityDataStatusResponse>(key),
      ),
    staleTime: 5 * 60_000,
    gcTime: 30 * 60_000,
    retry: retryEquityQuery,
  });
}

/** 构造一个真实日、周或月物理周期行情 Query。 */
export function equityBarsQueryOptions(
  exchange: EquityExchange,
  symbol: string,
  input: {
    dataVersion: string;
    factorDataVersion?: string;
    period: "1d" | "1w" | "1mo";
    start: string;
    end: string;
    adjust: "none" | "qfq" | "hfq";
    adjustAsOf?: string;
    cursor?: string;
    limit: number;
  },
) {
  const key = ["equity", exchange, symbol, "bars", input] as const;
  const path = withSearch(`/api/v1/equities/${exchange}/${symbol}/bars`, input);
  return queryOptions({
    queryKey: key,
    queryFn: () =>
      requestEquityEntity(path, undefined, equityBarPageSchema, previousEntity<EquityBarPage>(key)),
    staleTime: 5 * 60_000,
    gcTime: 30 * 60_000,
    retry: retryEquityQuery,
  });
}

/** 合并同一 cursor 链的行情页，并拒绝跨 publication、精确覆盖或非递增时间序列。 */
function mergeEquityBarPages(
  entities: ReadonlyArray<ConditionalEntity<EquityBarPage>>,
): EquityBarPage {
  const first = entities[0]?.body;
  const last = entities.at(-1)?.body;
  if (first === undefined || last === undefined) {
    throw new ApiError(502, "bar-pagination-empty");
  }

  const items = [];
  let priorPeriodEnd: string | undefined;
  for (const entity of entities) {
    const page = entity.body;
    if (
      page.exchange !== first.exchange ||
      page.symbol !== first.symbol ||
      page.period !== first.period ||
      page.adjustmentMode !== first.adjustmentMode ||
      page.adjustAsOf !== first.adjustAsOf ||
      page.factorVersion !== first.factorVersion ||
      page.dataVersion !== first.dataVersion ||
      page.coverageVersion !== first.coverageVersion ||
      page.publicationKind !== first.publicationKind ||
      page.sourceBatchId !== first.sourceBatchId
    ) {
      throw new ApiError(409, "snapshot-expired");
    }
    for (const item of page.items) {
      if (priorPeriodEnd !== undefined && item.periodEnd <= priorPeriodEnd) {
        throw new ApiError(502, "bar-pagination-order-invalid");
      }
      priorPeriodEnd = item.periodEnd;
      items.push(item);
    }
  }

  return { ...first, items, nextCursor: last.nextCursor };
}

/** 构造自动延续 opaque cursor 的完整 K 线窗口 Query。 */
export function equityBarsInfiniteQueryOptions(
  exchange: EquityExchange,
  symbol: string,
  input: {
    dataVersion: string;
    factorDataVersion?: string;
    period: "1d" | "1w" | "1mo";
    start: string;
    end: string;
    adjust: "none" | "qfq" | "hfq";
    adjustAsOf?: string;
    limit: number;
  },
) {
  const key = ["equity", exchange, symbol, "bars-window", input] as const;
  return infiniteQueryOptions({
    queryKey: key,
    initialPageParam: undefined as string | undefined,
    /** 每一页沿服务端签名 cursor 读取，并复用该页既有 ETag。 */
    queryFn: ({ pageParam }) => {
      const request = {
        ...input,
        ...(pageParam === undefined ? {} : { cursor: pageParam }),
      };
      const path = withSearch(`/api/v1/equities/${exchange}/${symbol}/bars`, request);
      return requestEquityEntity(
        path,
        undefined,
        equityBarPageSchema,
        previousInfiniteEntity<EquityBarPage>(key, pageParam),
      );
    },
    /** 受控页面预算足以覆盖完整 A 股历史，并防止异常 cursor 无限循环。 */
    getNextPageParam: (lastPage, allPages) =>
      allPages.length >= maximumBarPages ? undefined : (lastPage.body.nextCursor ?? undefined),
    /** Observer 只暴露跨页校验后的连续行情窗口。 */
    select: (data) => mergeEquityBarPages(data.pages),
    staleTime: 5 * 60_000,
    gcTime: 30 * 60_000,
    retry: retryEquityQuery,
  });
}

/** 构造绑定永久证券身份日期的公司概况 Query。 */
export function equityCompanyProfileQueryOptions(
  exchange: EquityExchange,
  symbol: string,
  input: { dataVersion: string; asOf?: string },
) {
  const key = ["equity", exchange, symbol, "company-profile", input] as const;
  return queryOptions({
    queryKey: key,
    queryFn: () =>
      requestEquityEntity(
        withSearch(`/api/v1/equities/${exchange}/${symbol}/company-profile`, input),
        undefined,
        equityCompanyProfileSchema,
        previousEntity<EquityCompanyProfile>(key),
      ),
    staleTime: 15 * 60_000,
    gcTime: 60 * 60_000,
    retry: retryEquityQuery,
  });
}

/** 合并同一公司行动 publication 的 cursor 页，并拒绝重复行动。 */
function mergeEquityCorporateActionPages(
  entities: ReadonlyArray<ConditionalEntity<EquityCorporateActionPage>>,
): EquityCorporateActionPage {
  const first = entities[0]?.body;
  const last = entities.at(-1)?.body;
  if (first === undefined || last === undefined) {
    throw new ApiError(502, "corporate-action-pagination-empty");
  }

  const items = [];
  const actionIdentifiers = new Set<string>();
  for (const entity of entities) {
    const page = entity.body;
    if (
      page.exchange !== first.exchange ||
      page.symbol !== first.symbol ||
      page.dataVersion !== first.dataVersion
    ) {
      throw new ApiError(409, "snapshot-expired");
    }
    for (const item of page.items) {
      if (actionIdentifiers.has(item.actionId)) {
        throw new ApiError(502, "corporate-action-pagination-duplicate");
      }
      actionIdentifiers.add(item.actionId);
      items.push(item);
    }
  }
  return { ...first, items, nextCursor: last.nextCursor };
}

/** 构造自动读取完整 cursor 链的公司行动 Query。 */
export function equityCorporateActionsInfiniteQueryOptions(
  exchange: EquityExchange,
  symbol: string,
  input: { dataVersion: string; start?: string; end?: string },
) {
  const requestInput = { ...input, limit: 100 };
  const key = ["equity", exchange, symbol, "corporate-actions", requestInput] as const;
  return infiniteQueryOptions({
    queryKey: key,
    initialPageParam: undefined as string | undefined,
    /** 公司行动逐页沿服务端 opaque cursor 读取。 */
    queryFn: ({ pageParam }) => {
      const request = {
        ...requestInput,
        ...(pageParam === undefined ? {} : { cursor: pageParam }),
      };
      const path = withSearch(`/api/v1/equities/${exchange}/${symbol}/corporate-actions`, request);
      return requestEquityEntity(
        path,
        undefined,
        equityCorporateActionPageSchema,
        previousInfiniteEntity<EquityCorporateActionPage>(key, pageParam),
      );
    },
    /** 400 条公司行动覆盖完整生命周期，并限制异常 cursor 扩张。 */
    getNextPageParam: (lastPage, allPages) =>
      allPages.length >= maximumCorporateActionPages
        ? undefined
        : (lastPage.body.nextCursor ?? undefined),
    /** 页面只消费完成 publication 和重复校验的行动集合。 */
    select: (data) => mergeEquityCorporateActionPages(data.pages),
    staleTime: 30 * 60_000,
    gcTime: 2 * 60 * 60_000,
    retry: retryEquityQuery,
  });
}

/** 构造统一事件页签 Query。 */
export function equityEventsQueryOptions(
  exchange: EquityExchange,
  symbol: string,
  input: EquityEventSearchRequest,
) {
  const key = ["equity", exchange, symbol, "events", input] as const;
  return queryOptions({
    queryKey: key,
    queryFn: () =>
      requestEquityEntity(
        `/api/v1/equities/${exchange}/${symbol}/events/search`,
        input,
        equityEventPageSchema,
        previousEntity<EquityEventPage>(key),
      ),
    placeholderData: keepPreviousData,
    staleTime: 30 * 60_000,
    gcTime: 2 * 60 * 60_000,
    retry: retryEquityQuery,
  });
}

/** 合并同一财务报告 publication 的 cursor 页，并拒绝重复报告。 */
function mergeEquityFinancialReportPages(
  entities: ReadonlyArray<ConditionalEntity<EquityFinancialReportPage>>,
): EquityFinancialReportPage {
  const first = entities[0]?.body;
  const last = entities.at(-1)?.body;
  if (first === undefined || last === undefined) {
    throw new ApiError(502, "financial-report-pagination-empty");
  }

  const items = [];
  const reportReferences = new Set<string>();
  for (const entity of entities) {
    const page = entity.body;
    if (
      page.exchange !== first.exchange ||
      page.symbol !== first.symbol ||
      page.methodologyCode !== first.methodologyCode ||
      page.methodologyVersion !== first.methodologyVersion ||
      page.dataVersion !== first.dataVersion
    ) {
      throw new ApiError(409, "snapshot-expired");
    }
    for (const item of page.items) {
      if (reportReferences.has(item.reportRef)) {
        throw new ApiError(502, "financial-report-pagination-duplicate");
      }
      reportReferences.add(item.reportRef);
      items.push(item);
    }
  }
  return { ...first, items, nextCursor: last.nextCursor };
}

/** 构造显式方法学下、自动读取完整 cursor 链的财务报告 Query。 */
export function equityFinancialReportsInfiniteQueryOptions(
  exchange: EquityExchange,
  symbol: string,
  dataVersion: string,
  methodologyCode: string,
  methodologyVersion: number,
  asOf?: string,
) {
  const input = { dataVersion, methodologyCode, methodologyVersion, asOf, limit: 50 };
  const key = ["equity", exchange, symbol, "financial-reports", input] as const;
  return infiniteQueryOptions({
    queryKey: key,
    initialPageParam: undefined as string | undefined,
    /** 报告列表逐页沿服务端 opaque cursor 读取。 */
    queryFn: ({ pageParam }) => {
      const request = {
        ...input,
        ...(pageParam === undefined ? {} : { cursor: pageParam }),
      };
      const path = withSearch(`/api/v1/equities/${exchange}/${symbol}/financial-reports`, request);
      return requestEquityEntity(
        path,
        undefined,
        equityFinancialReportPageSchema,
        previousInfiniteEntity<EquityFinancialReportPage>(key, pageParam),
      );
    },
    /** 800 份报告足以覆盖完整 A 股生命周期，异常 cursor 不会无限扩张。 */
    getNextPageParam: (lastPage, allPages) =>
      allPages.length >= maximumFinancialReportPages
        ? undefined
        : (lastPage.body.nextCursor ?? undefined),
    /** 页面只消费完成 publication 与重复校验的报告集合。 */
    select: (data) => mergeEquityFinancialReportPages(data.pages),
    staleTime: 30 * 60_000,
    gcTime: 2 * 60 * 60_000,
    retry: retryEquityQuery,
  });
}

/** 合并同一报告的行项目 cursor 页，并拒绝跨版本或重复指标。 */
function mergeEquityFinancialReportDetailPages(
  entities: ReadonlyArray<ConditionalEntity<EquityFinancialReportDetail>>,
): EquityFinancialReportDetail {
  const first = entities[0]?.body;
  const last = entities.at(-1)?.body;
  if (first === undefined || last === undefined) {
    throw new ApiError(502, "financial-report-detail-pagination-empty");
  }

  const items = [];
  const metricCodes = new Set<string>();
  for (const entity of entities) {
    const page = entity.body;
    if (
      page.report.reportRef !== first.report.reportRef ||
      page.dataVersion !== first.dataVersion
    ) {
      throw new ApiError(409, "snapshot-expired");
    }
    for (const item of page.items) {
      if (metricCodes.has(item.metricCode)) {
        throw new ApiError(502, "financial-report-detail-pagination-duplicate");
      }
      metricCodes.add(item.metricCode);
      items.push(item);
    }
  }
  return { ...first, items, nextCursor: last.nextCursor };
}

/** 构造一份公开财务报告、自动读取完整行项目 cursor 链的 Query。 */
export function equityFinancialReportDetailInfiniteQueryOptions(
  exchange: EquityExchange,
  symbol: string,
  reportRef: string,
  dataVersion: string,
  asOf?: string,
) {
  const key = [
    "equity",
    exchange,
    symbol,
    "financial-report",
    reportRef,
    dataVersion,
    asOf,
  ] as const;
  return infiniteQueryOptions({
    queryKey: key,
    initialPageParam: undefined as string | undefined,
    /** 报表行项目逐页沿服务端 opaque cursor 读取。 */
    queryFn: ({ pageParam }) => {
      const path = withSearch(
        `/api/v1/equities/${exchange}/${symbol}/financial-reports/${reportRef}`,
        {
          dataVersion,
          asOf,
          limit: 200,
          ...(pageParam === undefined ? {} : { cursor: pageParam }),
        },
      );
      return requestEquityEntity(
        path,
        undefined,
        equityFinancialReportDetailSchema,
        previousInfiniteEntity<EquityFinancialReportDetail>(key, pageParam),
      );
    },
    /** 800 个治理行项目提供硬上限，异常 cursor 不会无限读取。 */
    getNextPageParam: (lastPage, allPages) =>
      allPages.length >= maximumFinancialReportDetailPages
        ? undefined
        : (lastPage.body.nextCursor ?? undefined),
    /** 页面只消费同一报告和 publication 的完整行项目集合。 */
    select: (data) => mergeEquityFinancialReportDetailPages(data.pages),
    staleTime: 30 * 60_000,
    gcTime: 2 * 60 * 60_000,
    retry: retryEquityQuery,
  });
}

/** 合并同一财务指标 publication 的 cursor 页，并拒绝重复业务键。 */
function mergeEquityFinancialMetricPages(
  entities: ReadonlyArray<ConditionalEntity<EquityFinancialMetricPage>>,
): EquityFinancialMetricPage {
  const first = entities[0]?.body;
  const last = entities.at(-1)?.body;
  if (first === undefined || last === undefined) {
    throw new ApiError(502, "financial-metric-pagination-empty");
  }

  const items = [];
  const businessKeys = new Set<string>();
  for (const entity of entities) {
    const page = entity.body;
    if (
      page.exchange !== first.exchange ||
      page.symbol !== first.symbol ||
      page.origin !== first.origin ||
      page.methodologyCode !== first.methodologyCode ||
      page.methodologyVersion !== first.methodologyVersion ||
      page.dataVersion !== first.dataVersion
    ) {
      throw new ApiError(409, "snapshot-expired");
    }
    for (const item of page.items) {
      const key = [item.metricCode, item.reportPeriod, item.periodBasis, item.statementScope].join(
        ":",
      );
      if (businessKeys.has(key)) {
        throw new ApiError(502, "financial-metric-pagination-duplicate");
      }
      businessKeys.add(key);
      items.push(item);
    }
  }
  return { ...first, items, nextCursor: last.nextCursor };
}

/** 构造显式来源、方法学与指标集合的完整 cursor 链 Query。 */
export function equityFinancialMetricsInfiniteQueryOptions(
  exchange: EquityExchange,
  symbol: string,
  input: {
    dataVersion: string;
    origin: "PROVIDER_REPORTED" | "PLATFORM_DERIVED";
    methodologyCode: string;
    methodologyVersion: number;
    metric: string[];
    asOf?: string;
  },
) {
  const request = { ...input, limit: 500 };
  const key = ["equity", exchange, symbol, "financial-metrics", request] as const;
  return infiniteQueryOptions({
    queryKey: key,
    initialPageParam: undefined as string | undefined,
    /** 财务指标逐页沿服务端 opaque cursor 读取。 */
    queryFn: ({ pageParam }) => {
      const pageRequest = {
        ...request,
        ...(pageParam === undefined ? {} : { cursor: pageParam }),
      };
      const path = withSearch(
        `/api/v1/equities/${exchange}/${symbol}/financial-metrics`,
        pageRequest,
      );
      return requestEquityEntity(
        path,
        undefined,
        equityFinancialMetricPageSchema,
        previousInfiniteEntity<EquityFinancialMetricPage>(key, pageParam),
      );
    },
    /** 4,000 个指标观察覆盖完整生命周期并限制浏览器内存。 */
    getNextPageParam: (lastPage, allPages) =>
      allPages.length >= maximumFinancialMetricPages
        ? undefined
        : (lastPage.body.nextCursor ?? undefined),
    /** 页面只消费完成 publication 与业务键去重的指标集合。 */
    select: (data) => mergeEquityFinancialMetricPages(data.pages),
    staleTime: 30 * 60_000,
    gcTime: 2 * 60 * 60_000,
    retry: retryEquityQuery,
  });
}

/** 合并同一估值 publication 的 cursor 页，并拒绝跨版本或乱序数据。 */
function mergeEquityValuationPages(
  entities: ReadonlyArray<ConditionalEntity<EquityValuationPage>>,
): EquityValuationPage {
  const first = entities[0]?.body;
  const last = entities.at(-1)?.body;
  if (first === undefined || last === undefined) {
    throw new ApiError(502, "valuation-pagination-empty");
  }

  const items = [];
  let priorObservationDate: string | undefined;
  for (const entity of entities) {
    const page = entity.body;
    if (
      page.exchange !== first.exchange ||
      page.symbol !== first.symbol ||
      page.methodologyCode !== first.methodologyCode ||
      page.methodologyVersion !== first.methodologyVersion ||
      page.dataVersion !== first.dataVersion
    ) {
      throw new ApiError(409, "snapshot-expired");
    }
    for (const item of page.items) {
      if (priorObservationDate !== undefined && item.observationDate <= priorObservationDate) {
        throw new ApiError(502, "valuation-pagination-order-invalid");
      }
      priorObservationDate = item.observationDate;
      items.push(item);
    }
  }

  return { ...first, items, nextCursor: last.nextCursor };
}

/** 构造显式供应商方法学下、自动读取完整 cursor 链的历史估值 Query。 */
export function equityValuationsInfiniteQueryOptions(
  exchange: EquityExchange,
  symbol: string,
  input: {
    dataVersion: string;
    methodologyCode: string;
    methodologyVersion: number;
    metric: "market_cap" | "pe_ttm" | "pe_static" | "pb" | "pcf";
    start: string;
    end: string;
    asOf?: string;
  },
) {
  const request = { ...input, limit: 1000 };
  const key = ["equity", exchange, symbol, "valuations", request] as const;
  return infiniteQueryOptions({
    queryKey: key,
    initialPageParam: undefined as string | undefined,
    /** 每页使用服务端 opaque cursor，并按页复用强条件缓存实体。 */
    queryFn: ({ pageParam }) => {
      const pageRequest = {
        ...request,
        ...(pageParam === undefined ? {} : { cursor: pageParam }),
      };
      const path = withSearch(`/api/v1/equities/${exchange}/${symbol}/valuations`, pageRequest);
      return requestEquityEntity(
        path,
        undefined,
        equityValuationPageSchema,
        previousInfiniteEntity<EquityValuationPage>(key, pageParam),
      );
    },
    /** 3,660 日窗口最多四页；达到预算时停止，避免异常 cursor 无限读取。 */
    getNextPageParam: (lastPage, allPages) =>
      allPages.length >= maximumValuationPages
        ? undefined
        : (lastPage.body.nextCursor ?? undefined),
    /** 页面只消费通过 publication 和顺序校验后的完整估值序列。 */
    select: (data) => mergeEquityValuationPages(data.pages),
    staleTime: 30 * 60_000,
    gcTime: 2 * 60 * 60_000,
    retry: retryEquityQuery,
  });
}

/** 构造显式供应商方法学和分桶下的个股资金流 Query。 */
export function equityMoneyFlowQueryOptions(
  exchange: EquityExchange,
  symbol: string,
  input: {
    dataVersion: string;
    methodologyId: string;
    methodologyVersion: string;
    bucket: string;
    start: string;
    end: string;
  },
) {
  const { methodologyId, ...query } = input;
  const request = { ...query, limit: 500 };
  const key = ["equity", exchange, symbol, "money-flow", input] as const;
  const path = withSearch(
    `/api/v1/market/money-flow/methodologies/${methodologyId}/daily-series/equities/${exchange}/${symbol}`,
    request,
  );
  return queryOptions({
    queryKey: key,
    queryFn: () =>
      requestEquityEntity(
        path,
        undefined,
        equityMoneyFlowDailyPageSchema,
        previousEntity<EquityMoneyFlowDailyPage>(key),
      ),
    staleTime: 30 * 60_000,
    gcTime: 2 * 60 * 60_000,
    retry: retryEquityQuery,
  });
}

/** 读取并复验股票中心板块归属，禁止服务端把另一 publication 或身份日期放入当前缓存键。 */
async function requestExactEquitySectorEntity(
  path: string,
  key: readonly unknown[],
  expected: {
    exchange: EquityExchange;
    symbol: string;
    scheme: "eastmoney.industry" | "eastmoney.concept";
    dataVersion: string;
    identityAsOf: string;
  },
): Promise<ConditionalEntity<EquitySectorPage>> {
  const entity = await requestEquityEntity(
    path,
    undefined,
    equitySectorPageSchema,
    previousEntity<EquitySectorPage>(key),
  );
  if (
    entity.body.dataVersion !== expected.dataVersion ||
    entity.body.identityAsOf !== expected.identityAsOf ||
    entity.body.scheme !== expected.scheme ||
    entity.body.equity.exchange !== expected.exchange ||
    entity.body.equity.symbol !== expected.symbol
  ) {
    throw new ApiError(409, "snapshot-expired");
  }
  return entity;
}

/** 构造一个固定供应商分类体系的证券归属 Query。 */
export function equitySectorsQueryOptions(
  exchange: EquityExchange,
  symbol: string,
  scheme: "eastmoney.industry" | "eastmoney.concept",
  input: {
    dataVersion: string;
    identityAsOf: string;
    knownAt?: string;
  },
) {
  const key = ["equity", exchange, symbol, "sectors", scheme, input] as const;
  const path = withSearch(`/api/v1/market/equities/${exchange}/${symbol}/sectors`, {
    scheme,
    dataVersion: input.dataVersion,
    identityAsOf: input.identityAsOf,
    knownAt: input.knownAt,
    limit: 200,
  });
  return queryOptions({
    queryKey: key,
    /** 按 Query key 中的精确 publication 与身份日期读取并复验正文。 */
    queryFn: () =>
      requestExactEquitySectorEntity(path, key, {
        exchange,
        symbol,
        scheme,
        dataVersion: input.dataVersion,
        identityAsOf: input.identityAsOf,
      }),
    staleTime: 30 * 60_000,
    gcTime: 2 * 60 * 60_000,
    retry: retryEquityQuery,
  });
}
