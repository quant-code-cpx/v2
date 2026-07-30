import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useParams, useSearchParams } from "react-router-dom";

import {
  conditionalBody,
  equityBarsInfiniteQueryOptions,
  equityCompanyProfileQueryOptions,
  equityCorporateActionsInfiniteQueryOptions,
  equityDataStatusQueryOptions,
  equityEventsQueryOptions,
  equityFinancialMetricsInfiniteQueryOptions,
  equityFinancialReportsInfiniteQueryOptions,
  equityIdentityQueryOptions,
  equityListingHistoryQueryOptions,
  equityMoneyFlowQueryOptions,
  equitySearchQueryOptions,
  equitySectorsQueryOptions,
  equityValuationsInfiniteQueryOptions,
} from "../../../api/equity-market";
import { isApiError } from "../../../api/http";
import type {
  EquityDatasetStatus,
  EquityExchange,
  EquityListingStatus,
} from "../../../types/equity-market";
import {
  readEquityDetailUrl,
  writeEquityDetailUrl,
} from "../../EquityMarketView/utils/equity-market-url";
import type { EquityDetailUrlState } from "../../EquityMarketView/utils/equity-market-url";

/** 详情状态端点一次读取的全部独立数据族。 */
export const equityDetailDatasetFamilies = [
  "IDENTITY",
  "COMPANY_PROFILE",
  "BARS_1D",
  "BARS_1W",
  "BARS_1MO",
  "ADJUSTMENT_FACTOR",
  "CORPORATE_ACTION",
  "FINANCIAL_REPORT",
  "FINANCIAL_INDICATOR",
  "VALUATION",
  "MONEY_FLOW",
  "INDUSTRY_MEMBERSHIP",
  "CONCEPT_MEMBERSHIP",
  "SW_INDUSTRY_MEMBERSHIP",
  "EARNINGS_FORECAST",
  "EARNINGS_EXPRESS",
  "DRAGON_TIGER",
  "BLOCK_TRADE",
] as const;

/** 返回 Asia/Shanghai 当前日，只作为请求窗口边界，不宣称数据实时。 */
function currentShanghaiDate(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

/** 把 UTC 日期格式化为稳定的日期字符串。 */
function formatUtcDate(value: Date): string {
  return value.toISOString().slice(0, 10);
}

/** 从日期安全减去日历年，并把闰日收敛到目标月份最后一天。 */
function subtractCalendarYears(value: string, years: number): string {
  const [year, month, day] = value.split("-").map(Number);
  const targetYear = (year ?? 1970) - years;
  const targetMonth = month ?? 1;
  const lastDay = new Date(Date.UTC(targetYear, targetMonth, 0)).getUTCDate();
  return formatUtcDate(
    new Date(Date.UTC(targetYear, targetMonth - 1, Math.min(day ?? 1, lastDay))),
  );
}

/** 从日期减去固定自然日数，匹配 API 的最大 span 校验。 */
function subtractDays(value: string, days: number): string {
  const date = new Date(`${value}T00:00:00.000Z`);
  date.setUTCDate(date.getUTCDate() - days);
  return formatUtcDate(date);
}

/** 从结束日向前计算用户选择的历史窗口；all 表达证券可查全生命周期。 */
function dateWindow(
  range: EquityDetailUrlState["range"],
  identityAsOf?: string,
): { start: string; end: string } {
  const end = identityAsOf ?? currentShanghaiDate();
  if (range === "all") return { start: "1990-01-01", end };

  const years = range === "1y" ? 1 : 3;
  return { start: subtractCalendarYears(end, years), end };
}

/** 把用户窗口压到一个端点允许的最大自然日跨度。 */
function boundedDayWindow(
  window: { start: string; end: string },
  maximumDays: number,
): { start: string; end: string } {
  const boundary = subtractDays(window.end, maximumDays);
  return {
    start: window.start > boundary ? window.start : boundary,
    end: window.end,
  };
}

/** 把用户窗口压到事件端点允许的最大日历年跨度。 */
function boundedYearWindow(
  window: { start: string; end: string },
  maximumYears: number,
): { start: string; end: string } {
  const boundary = subtractCalendarYears(window.end, maximumYears);
  return {
    start: window.start > boundary ? window.start : boundary,
    end: window.end,
  };
}

/** 把所有叶子查询限制在已解析证券身份的有效期，防止代码复用跨证券读取。 */
function identityBoundWindow(
  window: { start: string; end: string },
  effectiveFrom: string | undefined,
  effectiveTo: string | null | undefined,
): { start: string; end: string } {
  const start =
    effectiveFrom !== undefined && effectiveFrom > window.start ? effectiveFrom : window.start;
  const inclusiveIdentityEnd =
    effectiveTo === null || effectiveTo === undefined ? window.end : subtractDays(effectiveTo, 1);
  const end = inclusiveIdentityEnd < window.end ? inclusiveIdentityEnd : window.end;
  return start <= end ? { start, end } : { start: end, end };
}

/** 在 data-status 响应中按稳定 family 查找一个数据集。 */
function findDataset(
  datasets: EquityDatasetStatus[] | undefined,
  family: string,
): EquityDatasetStatus | undefined {
  return datasets?.find(
    /** family 是详情查询的公开稳定身份，不使用物理 dataset 名猜测。 */
    (dataset) => dataset.family === family,
  );
}

/** 把字符串方法学版本安全解析为既有财务 API 所需的正整数。 */
function numericMethodologyVersion(value: string | undefined): number | undefined {
  if (value === undefined || !/^[1-9]\d*$/.test(value)) return undefined;
  const numeric = Number(value);
  return Number.isSafeInteger(numeric) ? numeric : undefined;
}

/** 判断叶子请求是否因 data-status publication 已切换而需要刷新状态。 */
function isSnapshotExpired(error: unknown): boolean {
  return isApiError(error) && error.status === 409 && error.code === "snapshot-expired";
}

/** 只把已有 producer 的生命周期状态带入详情 discovery，暂停上市保持失败关闭。 */
export function discoverableListingStatus(
  status: EquityListingStatus | undefined,
): "LISTED" | "DELISTED" | undefined {
  return status === "LISTED" || status === "DELISTED" ? status : undefined;
}

/** 判断事件翻页是否因 composite publication 切换而必须回到第一页。 */
export function shouldResetEventCursor(error: unknown, cursor: string | undefined): boolean {
  return cursor !== undefined && isSnapshotExpired(error);
}

/** 把物理 K 线周期映射到 data-status 的稳定数据族。 */
function barDatasetFamily(period: EquityDetailUrlState["period"]): string {
  return period === "1d" ? "BARS_1D" : period === "1w" ? "BARS_1W" : "BARS_1MO";
}

/** 管理个股 canonical 身份、页签 URL 和各数据集独立 TanStack Query。 */
export function useEquityDetail() {
  const parameters = useParams();
  const exchange = parameters.exchange as EquityExchange | undefined;
  const symbol = parameters.symbol ?? "";
  const validExchange = exchange === "SSE" || exchange === "SZSE" || exchange === "BSE";
  const validSymbol = /^\d{6}$/.test(symbol);
  const validIdentity = validExchange && validSymbol;
  const [searchParams, setSearchParams] = useSearchParams();
  const [eventCursor, setEventCursor] = useState<string | undefined>(undefined);
  const [eventPage, setEventPage] = useState(1);
  const state = useMemo(() => readEquityDetailUrl(searchParams), [searchParams]);

  // 详情 URL 只保留页签与明确图表控制，未知值会被规范替换。
  useEffect(() => {
    const normalized = writeEquityDetailUrl(state);
    if (normalized.toString() !== searchParams.toString()) {
      setSearchParams(normalized, { replace: true });
    }
  }, [searchParams, setSearchParams, state]);

  // 同一路径切换代码复用身份时清空旧事件 cursor，避免把旧证券页锚点带入新身份。
  useEffect(() => {
    setEventCursor(undefined);
    setEventPage(1);
  }, [exchange, state.identityAsOf, symbol]);

  const identityQuery = useQuery({
    ...equityIdentityQueryOptions(exchange ?? "SSE", symbol, state.identityAsOf),
    enabled: validIdentity,
  });
  const identity = conditionalBody(identityQuery.data);
  const identityReady = identityQuery.isSuccess && identity !== undefined;
  const resolvedIdentityAsOf = state.identityAsOf ?? identity?.effectiveAsOf;

  // 首次打开未带 asOf 的规范路由时固化已解析身份日期，刷新和分享不再重新猜测代码身份。
  useEffect(() => {
    if (state.identityAsOf === undefined && identity?.effectiveAsOf !== undefined) {
      setSearchParams(writeEquityDetailUrl({ ...state, identityAsOf: identity.effectiveAsOf }), {
        replace: true,
      });
    }
  }, [identity?.effectiveAsOf, setSearchParams, state]);

  const requestedWindow = useMemo(
    () => dateWindow(state.range, resolvedIdentityAsOf),
    [resolvedIdentityAsOf, state.range],
  );
  const window = useMemo(
    () =>
      identityBoundWindow(
        requestedWindow,
        identity?.identifier.effectiveFrom,
        identity?.identifier.effectiveTo,
      ),
    [identity?.identifier.effectiveFrom, identity?.identifier.effectiveTo, requestedWindow],
  );
  const valuationWindow = useMemo(() => boundedDayWindow(window, 3660), [window]);
  const moneyFlowWindow = useMemo(() => boundedDayWindow(window, 365), [window]);
  const eventWindow = useMemo(() => boundedYearWindow(window, 10), [window]);

  const statusQuery = useQuery({
    ...equityDataStatusQueryOptions(exchange ?? "SSE", symbol, {
      families: equityDetailDatasetFamilies,
      ...(resolvedIdentityAsOf === undefined ? {} : { asOf: resolvedIdentityAsOf }),
    }),
    enabled: validIdentity && identityReady,
  });
  const status = conditionalBody(statusQuery.data);

  const discoveryListingStatus = discoverableListingStatus(identity?.listing.status);
  const discoveryQuery = useQuery({
    ...equitySearchQueryOptions({
      q: symbol,
      exchanges: validExchange ? [exchange] : undefined,
      ...(discoveryListingStatus === undefined
        ? {}
        : { listingStatuses: [discoveryListingStatus] }),
      sort: [{ field: "symbol", direction: "ASC" }],
      limit: 3,
    }),
    enabled: validIdentity && identityReady && discoveryListingStatus !== undefined,
  });
  const discovery = conditionalBody(discoveryQuery.data);
  const discoveryRecord = discovery?.records.find(
    /** 头部报价和申万归属只使用 exchange+symbol 精确命中的 discovery 行。 */
    (record) =>
      record.identity.exchange === exchange &&
      record.identity.symbol === symbol &&
      (resolvedIdentityAsOf === undefined
        ? record.identity.name === identity?.name.value &&
          record.statuses.listingStatus === identity?.listing.status
        : record.identity.identityAsOf === resolvedIdentityAsOf),
  );

  const barStatus = findDataset(status?.datasets, barDatasetFamily(state.period));
  const barDataVersion = barStatus?.dataVersion ?? undefined;
  const adjustmentFactorStatus = findDataset(status?.datasets, "ADJUSTMENT_FACTOR");
  const factorDataVersion = adjustmentFactorStatus?.dataVersion ?? undefined;
  const barsQuery = useInfiniteQuery({
    ...equityBarsInfiniteQueryOptions(exchange ?? "SSE", symbol, {
      dataVersion: barDataVersion ?? "unavailable",
      ...(state.adjust === "none" ? {} : { factorDataVersion: factorDataVersion ?? "unavailable" }),
      period: state.period,
      ...window,
      adjust: state.adjust,
      ...(state.adjust === "none" ? {} : { adjustAsOf: window.end }),
      limit: state.range === "all" ? 2000 : state.range === "3y" ? 1000 : 500,
    }),
    enabled:
      validIdentity &&
      identityReady &&
      state.tab === "market" &&
      barStatus?.availability === "AVAILABLE" &&
      barDataVersion !== undefined &&
      (state.adjust === "none" ||
        (adjustmentFactorStatus?.availability === "AVAILABLE" && factorDataVersion !== undefined)),
  });
  const bars = barsQuery.data;

  // “全部”窗口会沿服务端签名 cursor 顺序取尽；每页仍受 2,000 条响应预算约束。
  useEffect(() => {
    if (
      state.tab === "market" &&
      barsQuery.hasNextPage &&
      !barsQuery.isFetchingNextPage &&
      !barsQuery.isFetchNextPageError
    ) {
      void barsQuery.fetchNextPage();
    }
  }, [
    barsQuery.fetchNextPage,
    barsQuery.hasNextPage,
    barsQuery.isFetchNextPageError,
    barsQuery.isFetchingNextPage,
    state.tab,
  ]);

  const corporateActionStatus = findDataset(status?.datasets, "CORPORATE_ACTION");
  const corporateActionsQuery = useInfiniteQuery({
    ...equityCorporateActionsInfiniteQueryOptions(exchange ?? "SSE", symbol, {
      dataVersion: corporateActionStatus?.dataVersion ?? "unavailable",
      start: window.start,
      end: window.end,
    }),
    enabled:
      validIdentity &&
      identityReady &&
      state.tab === "market" &&
      corporateActionStatus?.availability === "AVAILABLE" &&
      corporateActionStatus.dataVersion !== null &&
      corporateActionStatus.dataVersion !== undefined,
  });
  const corporateActions = corporateActionsQuery.data;

  // 公司行动沿同一 publication 自动取尽，避免长生命周期证券静默少展示记录。
  useEffect(() => {
    if (
      state.tab === "market" &&
      corporateActionsQuery.hasNextPage &&
      !corporateActionsQuery.isFetchingNextPage &&
      !corporateActionsQuery.isFetchNextPageError
    ) {
      void corporateActionsQuery.fetchNextPage();
    }
  }, [
    corporateActionsQuery.fetchNextPage,
    corporateActionsQuery.hasNextPage,
    corporateActionsQuery.isFetchNextPageError,
    corporateActionsQuery.isFetchingNextPage,
    state.tab,
  ]);

  const profileStatus = findDataset(status?.datasets, "COMPANY_PROFILE");
  const profileQuery = useQuery({
    ...equityCompanyProfileQueryOptions(exchange ?? "SSE", symbol, {
      dataVersion: profileStatus?.dataVersion ?? "unavailable",
      ...(resolvedIdentityAsOf === undefined ? {} : { asOf: resolvedIdentityAsOf }),
    }),
    enabled:
      validIdentity &&
      identityReady &&
      state.tab === "company" &&
      profileStatus?.availability === "AVAILABLE" &&
      profileStatus.dataVersion !== null &&
      profileStatus.dataVersion !== undefined,
  });
  const profile = conditionalBody(profileQuery.data);
  const listingHistoryQuery = useQuery({
    ...equityListingHistoryQueryOptions(exchange ?? "SSE", symbol, resolvedIdentityAsOf),
    enabled: validIdentity && identityReady && state.tab === "company",
  });
  const listingHistory = conditionalBody(listingHistoryQuery.data);

  const financialStatus = findDataset(status?.datasets, "FINANCIAL_REPORT");
  const financialVersion = numericMethodologyVersion(financialStatus?.methodology?.version);
  const financialQuery = useInfiniteQuery({
    ...equityFinancialReportsInfiniteQueryOptions(
      exchange ?? "SSE",
      symbol,
      financialStatus?.dataVersion ?? "unavailable",
      financialStatus?.methodology?.code ?? "unavailable",
      financialVersion ?? 1,
      resolvedIdentityAsOf,
    ),
    enabled:
      validIdentity &&
      identityReady &&
      state.tab === "financial" &&
      financialStatus?.availability === "AVAILABLE" &&
      financialStatus.dataVersion !== null &&
      financialStatus.dataVersion !== undefined &&
      financialVersion !== undefined,
  });
  const financialReports = financialQuery.data;

  // 财务报告列表沿同一 publication 自动取尽，避免只展示最近一页却没有提示。
  useEffect(() => {
    if (
      state.tab === "financial" &&
      financialQuery.hasNextPage &&
      !financialQuery.isFetchingNextPage &&
      !financialQuery.isFetchNextPageError
    ) {
      void financialQuery.fetchNextPage();
    }
  }, [
    financialQuery.fetchNextPage,
    financialQuery.hasNextPage,
    financialQuery.isFetchNextPageError,
    financialQuery.isFetchingNextPage,
    state.tab,
  ]);
  const financialMetricStatus = findDataset(status?.datasets, "FINANCIAL_INDICATOR");
  const financialMetricVersion = numericMethodologyVersion(
    financialMetricStatus?.methodology?.version,
  );
  const financialMetricsQuery = useInfiniteQuery({
    ...equityFinancialMetricsInfiniteQueryOptions(exchange ?? "SSE", symbol, {
      dataVersion: financialMetricStatus?.dataVersion ?? "unavailable",
      origin: "PLATFORM_DERIVED",
      methodologyCode: financialMetricStatus?.methodology?.code ?? "unavailable",
      methodologyVersion: financialMetricVersion ?? 1,
      ...(resolvedIdentityAsOf === undefined ? {} : { asOf: resolvedIdentityAsOf }),
      metric: [
        "platform.operating_revenue.single_quarter",
        "platform.operating_revenue.ttm",
        "platform.net_profit_parent.single_quarter",
        "platform.net_profit_parent.ttm",
      ],
    }),
    enabled:
      validIdentity &&
      identityReady &&
      state.tab === "financial" &&
      financialMetricStatus?.availability === "AVAILABLE" &&
      financialMetricStatus.dataVersion !== null &&
      financialMetricStatus.dataVersion !== undefined &&
      financialMetricStatus.methodology?.code === "platform.financial-derivation" &&
      financialMetricVersion !== undefined,
  });
  const financialMetrics = financialMetricsQuery.data;

  // 平台衍生指标沿同一 publication 自动取尽，图表和明细不会静默截断。
  useEffect(() => {
    if (
      state.tab === "financial" &&
      financialMetricsQuery.hasNextPage &&
      !financialMetricsQuery.isFetchingNextPage &&
      !financialMetricsQuery.isFetchNextPageError
    ) {
      void financialMetricsQuery.fetchNextPage();
    }
  }, [
    financialMetricsQuery.fetchNextPage,
    financialMetricsQuery.hasNextPage,
    financialMetricsQuery.isFetchNextPageError,
    financialMetricsQuery.isFetchingNextPage,
    state.tab,
  ]);

  const valuationStatus = findDataset(status?.datasets, "VALUATION");
  const valuationVersion = numericMethodologyVersion(valuationStatus?.methodology?.version);
  const valuationQuery = useInfiniteQuery({
    ...equityValuationsInfiniteQueryOptions(exchange ?? "SSE", symbol, {
      dataVersion: valuationStatus?.dataVersion ?? "unavailable",
      methodologyCode: valuationStatus?.methodology?.code ?? "unavailable",
      methodologyVersion: valuationVersion ?? 1,
      metric: "pe_ttm",
      ...valuationWindow,
      ...(resolvedIdentityAsOf === undefined ? {} : { asOf: resolvedIdentityAsOf }),
    }),
    enabled:
      validIdentity &&
      identityReady &&
      state.tab === "valuation" &&
      valuationStatus?.availability === "AVAILABLE" &&
      valuationStatus.dataVersion !== null &&
      valuationStatus.dataVersion !== undefined &&
      valuationVersion !== undefined,
  });
  const valuations = valuationQuery.data;

  // 估值图必须沿同一 publication 取尽受控窗口，不能把第一页误称为完整历史。
  useEffect(() => {
    if (
      state.tab === "valuation" &&
      valuationQuery.hasNextPage &&
      !valuationQuery.isFetchingNextPage &&
      !valuationQuery.isFetchNextPageError
    ) {
      void valuationQuery.fetchNextPage();
    }
  }, [
    state.tab,
    valuationQuery.fetchNextPage,
    valuationQuery.hasNextPage,
    valuationQuery.isFetchNextPageError,
    valuationQuery.isFetchingNextPage,
  ]);

  const moneyFlowStatus = findDataset(status?.datasets, "MONEY_FLOW");
  const moneyFlowQuery = useQuery({
    ...equityMoneyFlowQueryOptions(exchange ?? "SSE", symbol, {
      dataVersion: moneyFlowStatus?.dataVersion ?? "unavailable",
      methodologyId: moneyFlowStatus?.methodology?.code ?? "unavailable",
      methodologyVersion: moneyFlowStatus?.methodology?.version ?? "unavailable",
      bucket: "main",
      ...moneyFlowWindow,
    }),
    enabled:
      validIdentity &&
      identityReady &&
      state.tab === "money-flow" &&
      moneyFlowStatus?.availability === "AVAILABLE" &&
      moneyFlowStatus.dataVersion !== null &&
      moneyFlowStatus.dataVersion !== undefined &&
      moneyFlowStatus.methodology !== null &&
      moneyFlowStatus.methodology !== undefined,
  });
  const moneyFlow = conditionalBody(moneyFlowQuery.data);

  // 成分 leaf 必须同时绑定状态 publication 与路由身份日，否则同日重发或代码复用会串入另一证券。
  const industryStatus = findDataset(status?.datasets, "INDUSTRY_MEMBERSHIP");
  const industryDataVersion = industryStatus?.dataVersion ?? undefined;
  const industryQuery = useQuery({
    ...equitySectorsQueryOptions(exchange ?? "SSE", symbol, "eastmoney.industry", {
      dataVersion: industryDataVersion ?? "unavailable",
      identityAsOf: resolvedIdentityAsOf ?? "unavailable",
    }),
    enabled:
      validIdentity &&
      identityReady &&
      state.tab === "sectors" &&
      industryStatus?.availability === "AVAILABLE" &&
      industryDataVersion !== undefined &&
      resolvedIdentityAsOf !== undefined,
  });
  const industry = conditionalBody(industryQuery.data);
  const conceptStatus = findDataset(status?.datasets, "CONCEPT_MEMBERSHIP");
  const conceptDataVersion = conceptStatus?.dataVersion ?? undefined;
  const conceptQuery = useQuery({
    ...equitySectorsQueryOptions(exchange ?? "SSE", symbol, "eastmoney.concept", {
      dataVersion: conceptDataVersion ?? "unavailable",
      identityAsOf: resolvedIdentityAsOf ?? "unavailable",
    }),
    enabled:
      validIdentity &&
      identityReady &&
      state.tab === "sectors" &&
      conceptStatus?.availability === "AVAILABLE" &&
      conceptDataVersion !== undefined &&
      resolvedIdentityAsOf !== undefined,
  });
  const concepts = conditionalBody(conceptQuery.data);

  const eventsQuery = useQuery({
    ...equityEventsQueryOptions(exchange ?? "SSE", symbol, {
      families: [
        "CORPORATE_ACTION",
        "EARNINGS_FORECAST",
        "EARNINGS_EXPRESS",
        "DRAGON_TIGER",
        "BLOCK_TRADE",
      ],
      ...(resolvedIdentityAsOf === undefined ? {} : { asOf: resolvedIdentityAsOf }),
      ...eventWindow,
      ...(eventCursor === undefined ? {} : { cursor: eventCursor }),
      limit: 50,
    }),
    enabled: validIdentity && identityReady && state.tab === "events",
  });
  const events = conditionalBody(eventsQuery.data);

  const snapshotRecoveryVersion = status?.datasets
    .map((dataset) => `${dataset.family}:${dataset.dataVersion ?? "none"}`)
    .join("|");
  const snapshotRecoveryRef = useRef<string | undefined>(undefined);
  const snapshotError = [
    barsQuery.error,
    corporateActionsQuery.error,
    profileQuery.error,
    financialQuery.error,
    financialMetricsQuery.error,
    valuationQuery.error,
    moneyFlowQuery.error,
    industryQuery.error,
    conceptQuery.error,
    eventsQuery.error,
  ].find(isSnapshotExpired);

  // 事件 cursor 绑定独立 composite publication；版本切换后必须先回第一页再重试。
  useEffect(() => {
    if (!shouldResetEventCursor(eventsQuery.error, eventCursor)) return;
    setEventCursor(undefined);
    setEventPage(1);
  }, [eventCursor, eventsQuery.error]);

  // 每组旧 status 只自动恢复一次；新 status 会改变 query key 并以新版本重新读取叶子数据。
  useEffect(() => {
    if (
      snapshotError === undefined ||
      snapshotRecoveryVersion === undefined ||
      snapshotRecoveryRef.current === snapshotRecoveryVersion
    ) {
      return;
    }
    snapshotRecoveryRef.current = snapshotRecoveryVersion;
    void statusQuery.refetch();
  }, [snapshotError, snapshotRecoveryVersion, statusQuery.refetch]);

  /** 进入同一事件 publication 的下一 cursor 页。 */
  const nextEvents = useCallback(() => {
    const nextCursor = events?.page.nextCursor;
    if (nextCursor !== null && nextCursor !== undefined) {
      setEventCursor(nextCursor);
      setEventPage((current) => current + 1);
    }
  }, [events?.page.nextCursor]);

  /** 回到当前筛选事件第一页，不尝试反解 opaque cursor。 */
  const firstEvents = useCallback(() => {
    setEventCursor(undefined);
    setEventPage(1);
  }, []);

  /** 更新详情业务 URL，同时保留其余显式图表控制。 */
  const updateState = useCallback(
    (changes: Partial<EquityDetailUrlState>) => {
      if (changes.range !== undefined && changes.range !== state.range) {
        setEventCursor(undefined);
        setEventPage(1);
      }
      setSearchParams(writeEquityDetailUrl({ ...state, ...changes }));
    },
    [setSearchParams, state],
  );

  return {
    validIdentity,
    exchange,
    symbol,
    state,
    resolvedIdentityAsOf,
    window,
    valuationWindow,
    moneyFlowWindow,
    eventWindow,
    identityQuery,
    identity,
    statusQuery,
    status,
    discoveryQuery,
    discovery,
    discoveryRecord,
    barsQuery,
    bars,
    barStatus,
    adjustmentFactorStatus,
    corporateActionStatus,
    corporateActionsQuery,
    corporateActions,
    profileStatus,
    profileQuery,
    profile,
    listingHistoryQuery,
    listingHistory,
    financialStatus,
    financialQuery,
    financialReports,
    financialMetricStatus,
    financialMetricsQuery,
    financialMetrics,
    valuationStatus,
    valuationQuery,
    valuations,
    moneyFlowStatus,
    moneyFlowQuery,
    moneyFlow,
    industryStatus,
    industryQuery,
    industry,
    conceptStatus,
    conceptQuery,
    concepts,
    eventsQuery,
    events,
    eventPage,
    nextEvents,
    firstEvents,
    updateState,
  };
}

/** 暴露详情页签组件共享的页面模型类型。 */
export type EquityDetailModel = ReturnType<typeof useEquityDetail>;
