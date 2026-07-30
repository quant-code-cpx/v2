import { queryOptions } from "@tanstack/react-query";
import type { ZodType } from "zod";

import {
  marketIndexBarPageSchema,
  marketOverviewSchema,
  marketSectorBarPageSchema,
  marketSectorConstituentPageSchema,
  marketSectorEodPageSchema,
  marketSectorEodResourceSchema,
  marketSectorPageSchema,
  marketSectorMoneyFlowRankingPageSchema,
  marketSectorStrengthPageSchema,
  swIndustryBarPageSchema,
  swIndustryConstituentPageSchema,
  swIndustryPageSchema,
  swIndustryResourceSchema,
  swIndustryValuationSchema,
  swIndustryValuationPageSchema,
} from "../types/market";
import type {
  MarketIndexBarPage,
  MarketOverview,
  MarketSectorBarPage,
  MarketSectorConstituentPage,
  MarketSectorEodPage,
  MarketSectorEodResource,
  MarketSectorEodSort,
  MarketSectorPage,
  MarketSectorMoneyFlowRankingPage,
  MarketSectorScheme,
  MarketSectorStrengthPage,
  SwIndustryBarPage,
  SwIndustryConstituentPage,
  SwIndustryPage,
  SwIndustryResource,
  SwIndustryValuation,
  SwIndustryValuationPage,
} from "../types/market";
import { authSession } from "./auth-session";
import { ApiError, requestJsonResponse } from "./http";
import { queryClient } from "./query-client";

const overviewStaleTime = 5 * 60 * 1_000;
const eodStaleTime = 30 * 60 * 1_000;
const dailyGcTime = 24 * 60 * 60 * 1_000;
const activeMarketStateRefreshInterval = 60 * 1_000;
const inactiveMarketStateRefreshInterval = 5 * 60 * 1_000;
const stalePublicationRefreshInterval = 15 * 60 * 1_000;
const stalePublicationWindowStartMinute = 17 * 60 + 20;
const stalePublicationWindowEndMinute = 20 * 60;

/** 将通过严格合同校验的行情载荷与条件请求所需的 `ETag` 一同缓存。 */
export interface MarketQueryResult<T> {
  payload: T;
  etag: string;
}

/** 描述板块中心稳定 URL 所表达的横截面查询。 */
export interface MarketSectorListInput {
  scheme: MarketSectorScheme;
  asOf?: string;
  sort: MarketSectorEodSort;
  order: "asc" | "desc";
  query?: string;
  cursor?: string;
  limit: number;
}

/** 描述板块 K 线的后端原生周期窗口。 */
export interface MarketSectorBarsInput {
  scheme: MarketSectorScheme;
  sectorCode: string;
  period: "1d" | "1w" | "1mo";
  start: string;
  end: string;
  cursor?: string;
  limit: number;
}

/** 描述申万 taxonomy 的可分享筛选。 */
export interface SwIndustryListInput {
  snapshotDate?: string;
  level?: 1 | 2 | 3;
  parentCode?: string;
  cursor?: string;
  limit: number;
}

/** 描述通用条件 POST 所需的稳定路径、请求与响应合同。 */
interface ConditionalMarketRequest<T> {
  queryKey: readonly unknown[];
  path: string;
  schema: ZodType<T>;
  body?: unknown;
}

/** 从公开合同的顶层或 release 元数据读取稳定 publication 版本。 */
function payloadDataVersion(payload: unknown): string | undefined {
  if (typeof payload !== "object" || payload === null) {
    return undefined;
  }
  const record = payload as Record<string, unknown>;
  if (typeof record.dataVersion === "string") {
    return record.dataVersion;
  }
  if (typeof record.release !== "object" || record.release === null) {
    return undefined;
  }
  const release = record.release as Record<string, unknown>;
  return typeof release.dataVersion === "string" ? release.dataVersion : undefined;
}

/** 追加存在的查询值，防止 `undefined` 被编码为业务字符串。 */
function appendOptional(search: URLSearchParams, key: string, value: string | undefined): void {
  if (value !== undefined && value.length > 0) {
    search.set(key, value);
  }
}

/** 验证公开行情条件读取只接受 RFC 9110 强实体标签。 */
function isStrongEtag(value: string): boolean {
  return /^"[\x21\x23-\x7e]+"$/.test(value);
}

/** 校验 200/204 条件响应的合同、ETag 与 publication 版本绑定。 */
export function resolveConditionalMarketResponse<T>(
  data: unknown,
  headers: Headers,
  status: number,
  previous: MarketQueryResult<T> | undefined,
  schema: ZodType<T>,
): MarketQueryResult<T> {
  if (status !== 200 && status !== 204) {
    throw new ApiError(503, "conditional-status-invalid");
  }
  if (status === 204) {
    if (data !== undefined) {
      throw new ApiError(503, "conditional-204-body-invalid");
    }
    if (previous === undefined) {
      throw new ApiError(503, "conditional-cache-miss");
    }
    const responseEtag = headers.get("ETag");
    if (responseEtag === null || responseEtag !== previous.etag) {
      throw new ApiError(503, "conditional-cache-etag-mismatch");
    }
    const responseDataVersion = headers.get("X-Data-Version");
    const previousDataVersion = payloadDataVersion(previous.payload);
    if (
      responseDataVersion === null ||
      previousDataVersion === undefined ||
      responseDataVersion !== previousDataVersion
    ) {
      throw new ApiError(503, "conditional-cache-version-mismatch");
    }
    return previous;
  }
  if (data === undefined) {
    throw new ApiError(503, "conditional-entity-missing");
  }

  const parsed = schema.safeParse(data);
  if (!parsed.success) {
    throw new ApiError(503, "contract-invalid");
  }
  const etag = headers.get("ETag");
  if (etag === null) {
    throw new ApiError(503, "missing-etag");
  }
  if (!isStrongEtag(etag)) {
    throw new ApiError(503, "invalid-strong-etag");
  }
  const responseDataVersion = headers.get("X-Data-Version");
  const parsedDataVersion = payloadDataVersion(parsed.data);
  if (responseDataVersion === null || parsedDataVersion === undefined) {
    throw new ApiError(503, "missing-data-version");
  }
  if (responseDataVersion !== parsedDataVersion) {
    throw new ApiError(503, "data-version-mismatch");
  }

  return { payload: parsed.data, etag };
}

/** 发送认证条件 POST；204 只能复用同一 queryKey 下已验证的载荷。 */
async function conditionalMarketRequest<T>({
  queryKey,
  path,
  schema,
  body,
}: ConditionalMarketRequest<T>): Promise<MarketQueryResult<T>> {
  const previous = queryClient.getQueryData<MarketQueryResult<T>>(queryKey);

  return authSession.withAccessToken(
    /** 使用当前内存 token 发起幂等读请求，401 重放由会话协调器统一处理。 */
    async (accessToken) => {
      const response = await requestJsonResponse<unknown>(path, {
        headers: {
          Authorization: `Bearer ${accessToken}`,
          ...(previous === undefined ? {} : { "If-None-Match": previous.etag }),
        },
        body,
      });

      return resolveConditionalMarketResponse(
        response.data,
        response.headers,
        response.status,
        previous,
        schema,
      );
    },
  );
}

/** 读取上海时区的分钟位置，避免浏览器本地时区改变 EOD 轮询窗口。 */
function shanghaiMinuteOfDay(now: Date): number {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(now);
  const values = new Map(
    parts.map(
      /** 将格式化片段投影为稳定键值，忽略分隔符字面量。 */
      (part) => [part.type, part.value],
    ),
  );
  const hour = Number(values.get("hour"));
  const minute = Number(values.get("minute"));

  return hour * 60 + minute;
}

/** 按市场会话与 EOD 发布容量窗口计算 latest 条件刷新间隔。 */
export function marketOverviewRefetchInterval(
  asOf: string | undefined,
  status: MarketOverview["status"] | undefined,
  now = new Date(),
): number | false {
  if (asOf !== undefined || status === undefined) {
    return false;
  }
  if (status.freshness === "stale") {
    const minuteOfDay = shanghaiMinuteOfDay(now);
    if (minuteOfDay >= stalePublicationWindowEndMinute) {
      return false;
    }
    if (minuteOfDay >= stalePublicationWindowStartMinute) {
      return stalePublicationRefreshInterval;
    }
  }
  return status.marketState === "pre_open" ||
    status.marketState === "trading" ||
    status.marketState === "lunch_break"
    ? activeMarketStateRefreshInterval
    : inactiveMarketStateRefreshInterval;
}

/** 构造市场首页原子完整包 Query；页面永不读时拼接不同日期组件。 */
export function marketOverviewQueryOptions(asOf?: string) {
  const queryKey = ["market", "overview", asOf ?? "latest"] as const;
  return queryOptions({
    queryKey,
    /** 请求一个完整 publication，并用相同 queryKey 执行条件刷新。 */
    queryFn: () =>
      conditionalMarketRequest<MarketOverview>({
        queryKey,
        path: "/api/v1/market/overview",
        schema: marketOverviewSchema,
        body: asOf === undefined ? {} : { asOf },
      }),
    staleTime: asOf === undefined ? overviewStaleTime : Number.POSITIVE_INFINITY,
    gcTime: dailyGcTime,
    refetchOnWindowFocus: asOf === undefined,
    /** latest 按会话与 EOD 发布窗口条件重验；精确历史包不刷新。 */
    refetchInterval: (query) =>
      marketOverviewRefetchInterval(asOf, query.state.data?.payload.status),
  });
}

/** 构造固定指数的真实日 K 线 Query。 */
export function marketIndexBarsQueryOptions(input: {
  indexId: string;
  start: string;
  end: string;
  cursor?: string;
  limit: number;
}) {
  const queryKey = [
    "market",
    "index-bars",
    input.indexId,
    "1d",
    input.start,
    input.end,
    input.cursor ?? null,
    input.limit,
  ] as const;
  return queryOptions({
    queryKey,
    /** 请求服务端原生指数日线，不从成分观察或前端价格派生。 */
    queryFn: () =>
      conditionalMarketRequest<MarketIndexBarPage>({
        queryKey,
        path: `/api/v1/market/indices/${encodeURIComponent(input.indexId)}/bars`,
        schema: marketIndexBarPageSchema,
        body: {
          period: "1d",
          start: input.start,
          end: input.end,
          cursor: input.cursor,
          limit: input.limit,
        },
      }),
    staleTime: eodStaleTime,
    gcTime: dailyGcTime,
  });
}

/** 构造东财板块目录 Query，目录与 EOD 横截面保持独立失败边界。 */
export function marketSectorDirectoryQueryOptions(
  scheme: MarketSectorScheme,
  query?: string,
  cursor?: string,
) {
  const queryKey = ["market", "sectors", "directory", scheme, query ?? "", cursor ?? null] as const;
  const search = new URLSearchParams({ scheme, limit: "100" });
  appendOptional(search, "query", query);
  appendOptional(search, "cursor", cursor);
  return queryOptions({
    queryKey,
    /** 读取供应商目录 publication，不从排行结果反推完整 catalog。 */
    queryFn: () =>
      conditionalMarketRequest<MarketSectorPage>({
        queryKey,
        path: `/api/v1/market/sectors?${search.toString()}`,
        schema: marketSectorPageSchema,
      }),
    staleTime: eodStaleTime,
    gcTime: dailyGcTime,
  });
}

/** 构造同一 scheme、同一 EOD publication 的板块横截面 Query。 */
export function marketSectorEodQueryOptions(input: MarketSectorListInput) {
  const queryKey = [
    "market",
    "sectors",
    "eod",
    input.scheme,
    input.asOf ?? "latest",
    input.sort,
    input.order,
    input.cursor ?? null,
    input.limit,
  ] as const;
  const search = new URLSearchParams({
    scheme: input.scheme,
    sort: input.sort,
    order: input.order,
    limit: String(input.limit),
  });
  appendOptional(search, "asOf", input.asOf);
  appendOptional(search, "cursor", input.cursor);
  return queryOptions({
    queryKey,
    /** 请求一个不可变横截面，翻页不能跨越 dataVersion。 */
    queryFn: () =>
      conditionalMarketRequest<MarketSectorEodPage>({
        queryKey,
        path: `/api/v1/market/sectors/eod-snapshots?${search.toString()}`,
        schema: marketSectorEodPageSchema,
      }),
    staleTime: eodStaleTime,
    gcTime: dailyGcTime,
  });
}

/** 构造单板块 latest 或精确日期 EOD 快照 Query。 */
export function marketSectorSnapshotQueryOptions(
  scheme: MarketSectorScheme,
  sectorCode: string,
  asOf?: string,
) {
  const queryKey = ["market", "sector", scheme, sectorCode, "snapshot", asOf ?? "latest"] as const;
  const search = new URLSearchParams();
  appendOptional(search, "asOf", asOf);
  const suffix = search.size === 0 ? "" : `?${search.toString()}`;
  return queryOptions({
    queryKey,
    /** 读取精确板块快照，不以其他日期值填补指定日期。 */
    queryFn: () =>
      conditionalMarketRequest<MarketSectorEodResource>({
        queryKey,
        path: `/api/v1/market/sectors/${encodeURIComponent(scheme)}/${encodeURIComponent(
          sectorCode,
        )}/eod-snapshot${suffix}`,
        schema: marketSectorEodResourceSchema,
      }),
    staleTime: eodStaleTime,
    gcTime: dailyGcTime,
  });
}

/** 构造板块后端原生日、周或月 K 线 Query。 */
export function marketSectorBarsQueryOptions(input: MarketSectorBarsInput) {
  const queryKey = [
    "market",
    "sector",
    input.scheme,
    input.sectorCode,
    "bars",
    input.period,
    input.start,
    input.end,
    input.cursor ?? null,
    input.limit,
  ] as const;
  const search = new URLSearchParams({
    period: input.period,
    start: input.start,
    end: input.end,
    limit: String(input.limit),
  });
  appendOptional(search, "cursor", input.cursor);
  return queryOptions({
    queryKey,
    /** 请求来源直报物理周期，前端禁止用日线聚合周线或月线。 */
    queryFn: () =>
      conditionalMarketRequest<MarketSectorBarPage>({
        queryKey,
        path: `/api/v1/market/sectors/${encodeURIComponent(
          input.scheme,
        )}/${encodeURIComponent(input.sectorCode)}/bars?${search.toString()}`,
        schema: marketSectorBarPageSchema,
      }),
    staleTime: eodStaleTime,
    gcTime: dailyGcTime,
  });
}

/** 构造板块当前观察成分页 Query。 */
export function marketSectorConstituentsQueryOptions(
  scheme: MarketSectorScheme,
  sectorCode: string,
  asOf?: string,
  cursor?: string,
) {
  const queryKey = [
    "market",
    "sector",
    scheme,
    sectorCode,
    "constituents",
    asOf ?? "latest",
    cursor ?? null,
  ] as const;
  const search = new URLSearchParams({ limit: "100" });
  appendOptional(search, "asOf", asOf);
  appendOptional(search, "cursor", cursor);
  return queryOptions({
    queryKey,
    /** 读取 fixed release 的 verified 观察成分，不宣称正式历史调样日期。 */
    queryFn: () =>
      conditionalMarketRequest<MarketSectorConstituentPage>({
        queryKey,
        path: `/api/v1/market/sectors/${encodeURIComponent(
          scheme,
        )}/${encodeURIComponent(sectorCode)}/constituents?${search.toString()}`,
        schema: marketSectorConstituentPageSchema,
      }),
    staleTime: eodStaleTime,
    gcTime: dailyGcTime,
  });
}

/** 构造板块强弱及持续性 Query，window 明确进入缓存键与方法学边界。 */
export function marketSectorStrengthQueryOptions(input: {
  scheme: MarketSectorScheme;
  asOf?: string;
  window: 1 | 5 | 20;
  order: "asc" | "desc";
  cursor?: string;
  limit: number;
}) {
  const queryKey = [
    "market",
    "sectors",
    "strength",
    input.scheme,
    input.asOf ?? "latest",
    input.window,
    input.order,
    input.cursor ?? null,
    input.limit,
  ] as const;
  return queryOptions({
    queryKey,
    /** 请求已发布强弱结果，不在浏览器按不完整 K 线自行排名。 */
    queryFn: () =>
      conditionalMarketRequest<MarketSectorStrengthPage>({
        queryKey,
        path: "/api/v1/market/sectors/strength",
        schema: marketSectorStrengthPageSchema,
        body: input,
      }),
    staleTime: eodStaleTime,
    gcTime: dailyGcTime,
  });
}

/** 构造东财板块来源资金流排行 Query，方向和体系均进入缓存身份。 */
export function marketSectorMoneyFlowQueryOptions(input: {
  scheme: MarketSectorScheme;
  asOf?: string;
  order: "asc" | "desc";
  cursor?: string;
  limit: number;
}) {
  const queryKey = [
    "market",
    "sectors",
    "money-flow",
    input.scheme,
    input.asOf ?? "latest",
    input.order,
    input.cursor ?? null,
    input.limit,
  ] as const;
  return queryOptions({
    queryKey,
    /** 请求来源直报净额排行，不使用价格强弱推断资金方向。 */
    queryFn: () =>
      conditionalMarketRequest<MarketSectorMoneyFlowRankingPage>({
        queryKey,
        path: "/api/v1/market/sectors/money-flow-rankings",
        schema: marketSectorMoneyFlowRankingPageSchema,
        body: input,
      }),
    staleTime: eodStaleTime,
    gcTime: dailyGcTime,
  });
}

/** 构造申万 taxonomy Query，层级和父级均绑定 URL 与 queryKey。 */
export function swIndustryListQueryOptions(input: SwIndustryListInput) {
  const queryKey = [
    "market",
    "sw",
    "taxonomy",
    input.level ?? "all",
    input.parentCode ?? null,
    input.snapshotDate ?? "latest",
    input.cursor ?? null,
    input.limit,
  ] as const;
  const search = new URLSearchParams({ limit: String(input.limit) });
  appendOptional(search, "snapshotDate", input.snapshotDate);
  if (input.level !== undefined) search.set("level", String(input.level));
  appendOptional(search, "parentCode", input.parentCode);
  appendOptional(search, "cursor", input.cursor);
  return queryOptions({
    queryKey,
    /** 读取版本化 taxonomy，东财行业与申万节点绝不做名称等价映射。 */
    queryFn: () =>
      conditionalMarketRequest<SwIndustryPage>({
        queryKey,
        path: `/api/v1/market/industries/sw/list?${search.toString()}`,
        schema: swIndustryPageSchema,
      }),
    staleTime: eodStaleTime,
    gcTime: dailyGcTime,
  });
}

/** 构造申万估值观察页 Query，空值保留来源未报告语义。 */
export function swIndustryValuationsQueryOptions(input: {
  snapshotDate?: string;
  level?: 1 | 2 | 3;
  cursor?: string;
  limit: number;
}) {
  const queryKey = [
    "market",
    "sw",
    "valuations",
    input.level ?? "all",
    input.snapshotDate ?? "latest",
    input.cursor ?? null,
    input.limit,
  ] as const;
  const search = new URLSearchParams({ limit: String(input.limit) });
  appendOptional(search, "snapshotDate", input.snapshotDate);
  if (input.level !== undefined) search.set("level", String(input.level));
  appendOptional(search, "cursor", input.cursor);
  return queryOptions({
    queryKey,
    /** 读取供应商观察估值，不把未报告 TTM PE 或股息率补零。 */
    queryFn: () =>
      conditionalMarketRequest<SwIndustryValuationPage>({
        queryKey,
        path: `/api/v1/market/industries/sw/valuations?${search.toString()}`,
        schema: swIndustryValuationPageSchema,
      }),
    staleTime: eodStaleTime,
    gcTime: dailyGcTime,
  });
}

/** 构造申万节点与同版本父级闭包 Query。 */
export function swIndustryResourceQueryOptions(code: string, snapshotDate?: string) {
  const queryKey = ["market", "sw", code, "resource", snapshotDate ?? "latest"] as const;
  const search = new URLSearchParams();
  appendOptional(search, "snapshotDate", snapshotDate);
  const suffix = search.size === 0 ? "" : `?${search.toString()}`;
  return queryOptions({
    queryKey,
    /** 读取精确节点身份和父级闭包，不通过名称推断父子关系。 */
    queryFn: () =>
      conditionalMarketRequest<SwIndustryResource>({
        queryKey,
        path: `/api/v1/market/industries/sw/${encodeURIComponent(code)}${suffix}`,
        schema: swIndustryResourceSchema,
      }),
    staleTime: eodStaleTime,
    gcTime: dailyGcTime,
  });
}

/** 构造单个申万节点逐字段可解释的估值 Query。 */
export function swIndustryValuationQueryOptions(code: string, asOf?: string) {
  const queryKey = ["market", "sw", code, "valuation", asOf ?? "latest"] as const;
  return queryOptions({
    queryKey,
    /** 按代码读取同一节点估值，避免浏览器扫描层级分页。 */
    queryFn: () =>
      conditionalMarketRequest<SwIndustryValuation>({
        queryKey,
        path: `/api/v1/market/industries/sw/${encodeURIComponent(code)}/valuation`,
        schema: swIndustryValuationSchema,
        body: asOf === undefined ? {} : { asOf },
      }),
    staleTime: eodStaleTime,
    gcTime: dailyGcTime,
  });
}

/** 构造申万节点同步期已物化的日、周或月 K 线 Query。 */
export function swIndustryBarsQueryOptions(input: {
  code: string;
  period: "1d" | "1w" | "1mo";
  start: string;
  end: string;
  cursor?: string;
  limit: number;
}) {
  const queryKey = [
    "market",
    "sw",
    input.code,
    "bars",
    input.period,
    input.start,
    input.end,
    input.cursor ?? null,
    input.limit,
  ] as const;
  return queryOptions({
    queryKey,
    /** 请求申万已物化周期及方法学，不在前端聚合周月周期。 */
    queryFn: () =>
      conditionalMarketRequest<SwIndustryBarPage>({
        queryKey,
        path: `/api/v1/market/industries/sw/${encodeURIComponent(input.code)}/bars`,
        schema: swIndustryBarPageSchema,
        body: {
          period: input.period,
          start: input.start,
          end: input.end,
          cursor: input.cursor,
          limit: input.limit,
        },
      }),
    staleTime: eodStaleTime,
    gcTime: dailyGcTime,
  });
}

/** 构造申万正式成分页 Query。 */
export function swIndustryConstituentsQueryOptions(code: string, asOf?: string, cursor?: string) {
  const queryKey = [
    "market",
    "sw",
    code,
    "constituents",
    asOf ?? "latest",
    cursor ?? null,
  ] as const;
  return queryOptions({
    queryKey,
    /** 请求申万正式成员，不以东财同名板块成员替代。 */
    queryFn: () =>
      conditionalMarketRequest<SwIndustryConstituentPage>({
        queryKey,
        path: `/api/v1/market/industries/sw/${encodeURIComponent(code)}/constituents`,
        schema: swIndustryConstituentPageSchema,
        body: { asOf, cursor, limit: 100 },
      }),
    staleTime: eodStaleTime,
    gcTime: dailyGcTime,
  });
}
