import { keepPreviousData, queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import { authSession } from "./auth-session";
import { ApiError, requestJsonWithMetadata } from "./http";
import type {
  EtfDailyBarValues,
  EtfDatasetCode,
  EtfExchange,
  EtfListFilters,
  EtfNavValues,
  EtfProfileValues,
  EtfTradingStateValues,
  MarketDataPage,
} from "../types/etf";

/** service-api 对外暴露的唯一 typed market-data 查询边界。 */
const marketDataQueryPath = "/api/v1/market-data/query";

/** ETF 第一阶段固定使用的 dataset schema 版本。 */
const etfSchemaVersion = 2 as const;

/** ETF 产品目录 v2 的完整公开投影字段。 */
const profileFields = [
  "etfEntityRef",
  "exchange",
  "symbol",
  "displayName",
  "etfType",
  "managementMode",
  "managerName",
  "custodianName",
  "listedOn",
  "delistedOn",
  "listingStatus",
  "quoteCurrency",
  "navCurrency",
  "sourceTimePrecision",
] as const;

/** ETF 未复权日线 v2 的完整公开投影字段。 */
const barFields = [
  "tradeDate",
  "etfEntityRef",
  "open",
  "high",
  "low",
  "close",
  "volume",
  "volumeUnit",
  "amount",
  "currency",
  "tradeStatus",
  "adjustment",
] as const;

/** ETF 单位 NAV v2 的完整公开投影字段。 */
const navFields = ["navDate", "etfEntityRef", "navKind", "nav", "currency", "finality"] as const;

/** ETF 三维状态 v2 的完整公开投影字段。 */
const stateFields = [
  "etfEntityRef",
  "stateDimension",
  "state",
  "effectiveFrom",
  "effectiveTo",
  "reason",
] as const;

/** 合同日期使用不带时区的 ISO 日历日。 */
const isoDateSchema = z.string().regex(/^\d{4}-\d{2}-\d{2}$/u);

/** 关联标识复用服务边界允许的稳定安全字符，不强迫调用方必须生成 UUID。 */
const requestIdSchema = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/u);

/** 金融精度在 HTTP 边界保持十进制字符串，图表层才按需转为 number。 */
const decimalStringSchema = z.string().regex(/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/u);

/** ETF 产品目录 v2 业务字段合同。 */
const etfProfileValuesSchema = z
  .object({
    etfEntityRef: z.string().uuid(),
    exchange: z.enum(["SSE", "SZSE"]),
    symbol: z.string().regex(/^\d{6}$/u),
    displayName: z.string().trim().min(1).max(160),
    etfType: z.string().trim().min(1).max(80),
    managementMode: z.string().trim().min(1).max(80),
    managerName: z.string().trim().min(1).max(160).nullable(),
    custodianName: z.string().trim().min(1).max(160).nullable(),
    listedOn: isoDateSchema.nullable(),
    delistedOn: isoDateSchema.nullable(),
    listingStatus: z.enum(["LISTED", "SUSPENDED", "DELISTED", "UNKNOWN"]),
    quoteCurrency: z.string().regex(/^[A-Z]{3}$/u),
    navCurrency: z.string().regex(/^[A-Z]{3}$/u),
    sourceTimePrecision: z.string().trim().min(1).max(40),
  })
  .strict();

/** ETF 未复权日线 v2 业务字段合同。 */
const etfDailyBarValuesSchema = z
  .object({
    tradeDate: isoDateSchema,
    etfEntityRef: z.string().uuid(),
    open: decimalStringSchema,
    high: decimalStringSchema,
    low: decimalStringSchema,
    close: decimalStringSchema,
    volume: decimalStringSchema,
    volumeUnit: z.string().trim().min(1).max(40),
    amount: decimalStringSchema,
    currency: z.string().regex(/^[A-Z]{3}$/u),
    tradeStatus: z.string().trim().min(1).max(80).nullable(),
    adjustment: z.literal("UNADJUSTED"),
  })
  .strict();

/** ETF NAV 日值 v2 业务字段合同。 */
const etfNavValuesSchema = z
  .object({
    navDate: isoDateSchema,
    etfEntityRef: z.string().uuid(),
    navKind: z.enum(["UNIT", "ACCUMULATED"]),
    nav: decimalStringSchema,
    currency: z.string().regex(/^[A-Z]{3}$/u),
    finality: z.enum(["FINAL", "PROVISIONAL", "UNKNOWN"]),
  })
  .strict();

/** ETF 日级交易、申购和赎回状态 v2 业务字段合同。 */
const etfTradingStateValuesSchema = z
  .object({
    etfEntityRef: z.string().uuid(),
    stateDimension: z.enum(["TRADING", "SUBSCRIPTION", "REDEMPTION"]),
    state: z.string().trim().min(1).max(80),
    effectiveFrom: isoDateSchema,
    effectiveTo: isoDateSchema.nullable(),
    reason: z.string().trim().min(1).max(500).nullable(),
  })
  .strict();

/** typed record 公开来源的严格合同。 */
const releaseSourceSchema = z
  .object({
    sourceRef: z.string().min(1),
    publisher: z.string().min(1),
    sourceDataset: z.string().min(1),
    authoritative: z.boolean(),
    redistribution: z.string().min(1),
    coverageNote: z.string().nullable(),
  })
  .strict();

/** 有 publication 时的严格发布元数据。 */
const availableReleaseSchema = z
  .object({
    dataVersion: z.string().uuid(),
    publishedAt: z.string().datetime({ offset: true }),
    knowledgeCutoff: z.string().datetime({ offset: true }),
    publicUsableAt: z.string().datetime({ offset: true }),
    effectiveFrom: z.string().datetime({ offset: true }).nullable(),
    effectiveTo: z.string().datetime({ offset: true }).nullable(),
    methodology: z.record(z.string(), z.unknown()),
    sources: z.array(releaseSourceSchema),
    quality: z.record(z.string(), z.unknown()),
    completeness: z.enum(["COMPLETE", "PARTIAL", "UNKNOWN"]),
    disclaimers: z.array(z.string()).optional(),
  })
  .strict();

/** 尚无 publication 时的成功空结果元数据。 */
const emptyReleaseSchema = z
  .object({
    state: z.enum(["EMPTY", "SOURCE_UNAVAILABLE", "CURRENTLY_UNSUPPORTED"]),
    observedAt: z.string().datetime({ offset: true }).nullable(),
    reasonCode: z.enum([
      "NO_MATCHING_FACTS",
      "PROVIDER_UNAVAILABLE",
      "CAPABILITY_NOT_CONFIGURED",
      "PUBLICATION_NOT_AVAILABLE",
      "NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET",
    ]),
  })
  .strict();

/** 来源不可用状态允许的稳定公开原因，不接受下游自由文本。 */
const sourceUnavailableReasonCodes = new Set([
  "PROVIDER_UNAVAILABLE",
  "CAPABILITY_NOT_CONFIGURED",
  "PUBLICATION_NOT_AVAILABLE",
]);

/** typed market-data 请求中的有限标量过滤条件。 */
interface MarketDataFilterInput {
  field: string;
  operator: "EQ" | "IN" | "GTE" | "LTE" | "RANGE" | "PREFIX" | "CONTAINS";
  values: readonly (string | number | boolean)[];
}

/** typed market-data 请求体；业务调用方只能由本模块构造。 */
interface MarketDataQueryInput {
  dataset: { code: EtfDatasetCode; schemaVersion: typeof etfSchemaVersion };
  businessScope: "ETF";
  time: {
    dimension: "EFFECTIVE_AT" | "TRADE_DATE";
    from: string;
    to: string;
    timezone: "Asia/Shanghai";
  };
  visibility: { mode: "CURRENT" };
  selection: { qualityStatuses: readonly ["PASSED", "WARNED"] };
  fields: readonly string[];
  filters: readonly MarketDataFilterInput[];
  sort: readonly {
    field: string;
    direction: "ASC" | "DESC";
  }[];
  page: { limit: number; cursor?: string };
}

/** 构造与指定 dataset 和业务字段绑定的标准 record envelope schema。 */
function createMarketDataPageSchema<TValues>(
  datasetCode: EtfDatasetCode,
  valuesSchema: z.ZodType<TValues>,
) {
  const recordSchema = z
    .object({
      recordRef: z.string().min(1),
      recordType: z.string().min(1),
      entity: z
        .object({
          entityRef: z.string().min(1),
          entityType: z.string().min(1),
          identifiers: z.array(z.unknown()),
        })
        .strict(),
      time: z.record(z.string(), z.unknown()),
      publicUsableAt: z.string().datetime({ offset: true }),
      availabilityBasis: z.string().min(1),
      sourcePublishedAt: z.string().datetime({ offset: true }).nullable(),
      observedAt: z.string().datetime({ offset: true }),
      dataVersion: z.string().uuid(),
      sourceRef: z.string().min(1),
      methodologyVersion: z.string().min(1),
      qualityStatus: z.string().min(1),
      revision: z
        .object({
          revisionNumber: z.number().int().positive(),
          currentInPublication: z.boolean(),
        })
        .strict(),
      values: valuesSchema,
    })
    .strict();

  const pageSchema = z
    .object({
      meta: z
        .object({
          requestId: requestIdSchema,
          contractVersion: z.literal("1.0.0"),
          dataset: z
            .object({
              code: z.literal(datasetCode),
              schemaVersion: z.literal(etfSchemaVersion),
            })
            .strict(),
          availability: z.enum([
            "AVAILABLE",
            "EMPTY",
            "SOURCE_UNAVAILABLE",
            "CURRENTLY_UNSUPPORTED",
          ]),
          release: z.union([availableReleaseSchema, emptyReleaseSchema]),
          visibility: z.record(z.string(), z.unknown()),
          page: z
            .object({
              limit: z.number().int().positive().max(500),
              hasMore: z.boolean(),
              nextCursor: z.string().nullable(),
            })
            .strict(),
          coverage: z.record(z.string(), z.unknown()),
          warnings: z.array(z.string()),
          disclaimers: z.array(z.string()),
        })
        .strict(),
      records: z.array(recordSchema).max(500),
    })
    .strict();

  return pageSchema.superRefine(
    /** 将 availability、空 release 原因和唯一可不支持的 NAV dataset 绑定为同一合同。 */
    (page, context) => {
      const { availability, release } = page.meta;
      if (availability === "AVAILABLE") {
        if (!("dataVersion" in release)) {
          context.addIssue({
            code: "custom",
            message: "available ETF page requires publication metadata",
          });
        }
        return;
      }
      if ("dataVersion" in release) {
        context.addIssue({
          code: "custom",
          message: "unavailable ETF page cannot carry publication metadata",
        });
        return;
      }
      const validReason =
        release.state === availability &&
        ((availability === "EMPTY" && release.reasonCode === "NO_MATCHING_FACTS") ||
          (availability === "SOURCE_UNAVAILABLE" &&
            sourceUnavailableReasonCodes.has(release.reasonCode)) ||
          (availability === "CURRENTLY_UNSUPPORTED" &&
            datasetCode === "fund.etf.nav.1d.reported" &&
            release.reasonCode === "NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET"));
      if (
        !validReason ||
        page.records.length > 0 ||
        page.meta.page.hasMore ||
        page.meta.page.nextCursor !== null
      ) {
        context.addIssue({
          code: "custom",
          message: "ETF empty result does not match dataset, state, reason, or page",
        });
      }
    },
  );
}

/** 返回给定时刻在 `Asia/Shanghai` 对应的 ISO 日历日。 */
export function shanghaiCalendarDate(reference = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(reference);
  /** 按日期部件类型读取格式化结果，避免依赖本地化分隔符。 */
  const readPart = (type: Intl.DateTimeFormatPartTypes): string =>
    parts.find(
      /** 找到目标日期部件。 */
      (part) => part.type === type,
    )?.value ?? "";

  return `${readPart("year")}-${readPart("month")}-${readPart("day")}`;
}

/** 从 ISO 日历日向前移动固定天数，供有界日线和 NAV 查询复用。 */
function subtractCalendarDays(isoDate: string, days: number): string {
  const [year, month, day] = isoDate.split("-").map(
    /** 日期组件由受控 ISO 字符串转为十进制整数。 */
    (value) => Number.parseInt(value, 10),
  );
  if (year === undefined || month === undefined || day === undefined) {
    throw new TypeError("ETF 查询日期无效。");
  }
  const shifted = new Date(Date.UTC(year, month - 1, day - days));

  return shifted.toISOString().slice(0, 10);
}

/** 构造所有 ETF 查询共享的当前可见性与质量选择。 */
function createBaseQuery(
  datasetCode: EtfDatasetCode,
  dimension: "EFFECTIVE_AT" | "TRADE_DATE",
  from: string,
  to: string,
): Pick<MarketDataQueryInput, "dataset" | "businessScope" | "time" | "visibility" | "selection"> {
  return {
    dataset: { code: datasetCode, schemaVersion: etfSchemaVersion },
    businessScope: "ETF",
    time: { dimension, from, to, timezone: "Asia/Shanghai" },
    visibility: { mode: "CURRENT" },
    selection: { qualityStatuses: ["PASSED", "WARNED"] },
  };
}

/** 通过共享 POST 传输层执行查询并严格验证 typed envelope 与业务字段。 */
async function executeEtfQuery<TValues>(
  input: MarketDataQueryInput,
  valuesSchema: z.ZodType<TValues>,
  signal?: AbortSignal,
): Promise<MarketDataPage<TValues>> {
  return authSession.withAccessToken(async (accessToken) => {
    const response = await requestJsonWithMetadata<unknown>(marketDataQueryPath, {
      headers: { Authorization: `Bearer ${accessToken}` },
      body: input,
      signal,
    });
    const result = createMarketDataPageSchema(input.dataset.code, valuesSchema).safeParse(
      response.data,
    );

    if (!result.success) {
      throw new ApiError(502, "market-data-contract-mismatch");
    }
    if (result.data.meta.availability !== "AVAILABLE" && result.data.records.length > 0) {
      throw new ApiError(502, "market-data-availability-mismatch");
    }
    if (
      result.data.meta.availability === "AVAILABLE" &&
      !("dataVersion" in result.data.meta.release)
    ) {
      throw new ApiError(502, "market-data-release-mismatch");
    }

    return result.data as MarketDataPage<TValues>;
  });
}

/** 根据列表 URL 构造单一真实关键词过滤器，不把关键词解释为基金类别。 */
function keywordFilter(query: string | undefined): MarketDataFilterInput | undefined {
  if (query === undefined) {
    return undefined;
  }

  return /^\d{1,6}$/u.test(query)
    ? { field: "symbol", operator: "PREFIX", values: [query] }
    : { field: "displayName", operator: "CONTAINS", values: [query] };
}

/** 查询一个交易所分区内的 ETF 产品目录页。 */
export async function queryEtfProfiles(
  filters: EtfListFilters,
  signal?: AbortSignal,
): Promise<MarketDataPage<EtfProfileValues>> {
  const today = shanghaiCalendarDate();
  const filtersInput: MarketDataFilterInput[] = [
    { field: "exchange", operator: "EQ", values: [filters.exchange] },
  ];
  const searchFilter = keywordFilter(filters.q);
  if (searchFilter !== undefined) {
    filtersInput.push(searchFilter);
  }

  return executeEtfQuery(
    {
      ...createBaseQuery("fund.etf.profile.reported", "EFFECTIVE_AT", today, today),
      fields: profileFields,
      filters: filtersInput,
      sort: [
        { field: filters.sort, direction: filters.order.toUpperCase() as "ASC" | "DESC" },
        { field: "etfEntityRef", direction: "ASC" },
      ],
      page: {
        limit: filters.pageSize,
        ...(filters.cursor === undefined ? {} : { cursor: filters.cursor }),
      },
    },
    etfProfileValuesSchema,
    signal,
  );
}

/** 按交易所和精确代码查询一个 ETF 身份，不从目录缺席推断退市。 */
export async function queryEtfProfile(
  exchange: EtfExchange,
  symbol: string,
  signal?: AbortSignal,
): Promise<MarketDataPage<EtfProfileValues>> {
  const today = shanghaiCalendarDate();

  return executeEtfQuery(
    {
      ...createBaseQuery("fund.etf.profile.reported", "EFFECTIVE_AT", today, today),
      fields: profileFields,
      filters: [
        { field: "exchange", operator: "EQ", values: [exchange] },
        { field: "symbol", operator: "EQ", values: [symbol] },
      ],
      sort: [
        { field: "symbol", direction: "ASC" },
        { field: "etfEntityRef", direction: "ASC" },
      ],
      page: { limit: 2 },
    },
    etfProfileValuesSchema,
    signal,
  );
}

/** 查询一个 ETF 最近 365 个日历日的未复权日线。 */
export async function queryEtfDailyBars(
  etfEntityRef: string,
  signal?: AbortSignal,
): Promise<MarketDataPage<EtfDailyBarValues>> {
  const to = shanghaiCalendarDate();
  const from = subtractCalendarDays(to, 365);

  return executeEtfQuery(
    {
      ...createBaseQuery("fund.etf.bar.1d.reported", "TRADE_DATE", from, to),
      fields: barFields,
      filters: [{ field: "etfEntityRef", operator: "EQ", values: [etfEntityRef] }],
      sort: [{ field: "tradeDate", direction: "ASC" }],
      page: { limit: 366 },
    },
    etfDailyBarValuesSchema,
    signal,
  );
}

/** 查询一个 ETF 最近 365 个日历日的单位 NAV，不混入累计 NAV。 */
export async function queryEtfUnitNavs(
  etfEntityRef: string,
  signal?: AbortSignal,
): Promise<MarketDataPage<EtfNavValues>> {
  const to = shanghaiCalendarDate();
  const from = subtractCalendarDays(to, 365);

  return executeEtfQuery(
    {
      ...createBaseQuery("fund.etf.nav.1d.reported", "TRADE_DATE", from, to),
      fields: navFields,
      filters: [
        { field: "etfEntityRef", operator: "EQ", values: [etfEntityRef] },
        { field: "navKind", operator: "EQ", values: ["UNIT"] },
      ],
      sort: [{ field: "navDate", direction: "ASC" }],
      page: { limit: 366 },
    },
    etfNavValuesSchema,
    signal,
  );
}

/** 查询一个 ETF 最近 365 日由来源报告的交易、申购和赎回独立状态。 */
export async function queryEtfTradingStates(
  etfEntityRef: string,
  signal?: AbortSignal,
): Promise<MarketDataPage<EtfTradingStateValues>> {
  const to = shanghaiCalendarDate();
  const from = subtractCalendarDays(to, 365);

  return executeEtfQuery(
    {
      ...createBaseQuery("fund.etf.trading_state.reported", "EFFECTIVE_AT", from, to),
      fields: stateFields,
      filters: [{ field: "etfEntityRef", operator: "EQ", values: [etfEntityRef] }],
      sort: [{ field: "effectiveFrom", direction: "DESC" }],
      page: { limit: 500 },
    },
    etfTradingStateValuesSchema,
    signal,
  );
}

/** 构造 ETF 产品目录查询，筛选、游标和排序共同进入 Query key。 */
export function etfProfileListQueryOptions(filters: EtfListFilters) {
  return queryOptions({
    queryKey: ["market-data", "etf", "profiles", filters] as const,
    /** 将 TanStack Query 的取消信号传递到共享浏览器传输层。 */
    queryFn: ({ signal }) => queryEtfProfiles(filters, signal),
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  });
}

/** 构造 ETF 产品详情身份查询。 */
export function etfProfileQueryOptions(exchange: EtfExchange, symbol: string) {
  return queryOptions({
    queryKey: ["market-data", "etf", "profile", exchange, symbol] as const,
    /** 将详情请求取消信号传递到共享浏览器传输层。 */
    queryFn: ({ signal }) => queryEtfProfile(exchange, symbol, signal),
    staleTime: 60_000,
  });
}

/** 构造 ETF 日线查询；身份缺失时由页面 Hook 禁止执行。 */
export function etfDailyBarsQueryOptions(etfEntityRef: string) {
  return queryOptions({
    queryKey: ["market-data", "etf", "bars", etfEntityRef] as const,
    /** 将日线请求取消信号传递到共享浏览器传输层。 */
    queryFn: ({ signal }) => queryEtfDailyBars(etfEntityRef, signal),
    staleTime: 5 * 60_000,
  });
}

/** 构造 ETF 单位 NAV 查询。 */
export function etfUnitNavsQueryOptions(etfEntityRef: string) {
  return queryOptions({
    queryKey: ["market-data", "etf", "navs", etfEntityRef] as const,
    /** 将 NAV 请求取消信号传递到共享浏览器传输层。 */
    queryFn: ({ signal }) => queryEtfUnitNavs(etfEntityRef, signal),
    staleTime: 5 * 60_000,
  });
}

/** 构造 ETF 三维状态查询。 */
export function etfTradingStatesQueryOptions(etfEntityRef: string) {
  return queryOptions({
    queryKey: ["market-data", "etf", "states", etfEntityRef] as const,
    /** 将状态请求取消信号传递到共享浏览器传输层。 */
    queryFn: ({ signal }) => queryEtfTradingStates(etfEntityRef, signal),
    staleTime: 60_000,
  });
}
