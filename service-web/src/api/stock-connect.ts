import type { z } from "zod";

import {
  stockConnectActiveSecurityPageSchema,
  stockConnectActiveSecurityQuerySchema,
  stockConnectChannelQuerySchema,
  stockConnectChannelResponseSchema,
  stockConnectOverviewQuerySchema,
  stockConnectOverviewResponseSchema,
  stockConnectReadinessQuerySchema,
  stockConnectReadinessResponseSchema,
  stockConnectSecurityContextQuerySchema,
  stockConnectSecurityContextResponseSchema,
} from "../types/stock-connect";
import type {
  StockConnectActiveSecurityPage,
  StockConnectActiveSecurityQuery,
  StockConnectChannelQuery,
  StockConnectChannelResponse,
  StockConnectOverviewQuery,
  StockConnectOverviewResponse,
  StockConnectReadinessQuery,
  StockConnectReadinessResponse,
  StockConnectSecurityContextQuery,
  StockConnectSecurityContextResponse,
  VersionedStockConnectResponse,
} from "../types/stock-connect";
import { authSession } from "./auth-session";
import { ApiError, requestJsonResponse } from "./http";

/** 描述条件读取可复用的上一不可变 publication 与取消信号。 */
export interface StockConnectReadOptions<T> {
  previous?: VersionedStockConnectResponse<T>;
  signal?: AbortSignal;
}

/** 描述一个受 Zod 合同保护的沪深港通 POST 读取。 */
interface StockConnectReadContract<TRequest, TResponse> {
  path: string;
  requestSchema: z.ZodType<TRequest>;
  responseSchema: z.ZodType<TResponse>;
  dataVersion: (response: TResponse) => string;
}

/** 执行单个真实公开 POST，并验证响应体、版本头和 204 条件命中语义。 */
async function readStockConnectResource<TRequest, TResponse>(
  contract: StockConnectReadContract<TRequest, TResponse>,
  request: TRequest,
  options: StockConnectReadOptions<TResponse>,
): Promise<VersionedStockConnectResponse<TResponse>> {
  const body = contract.requestSchema.parse(request);

  /** 使用会话协调器取得短期 token；生产数据仍只来自 service-api。 */
  return authSession.withAccessToken(async (accessToken) => {
    const headers = new Headers({
      Authorization: `Bearer ${accessToken}`,
      "X-Request-Id": globalThis.crypto.randomUUID(),
    });

    if (options.previous !== undefined) {
      headers.set("If-None-Match", options.previous.etag);
    }

    const response = await requestJsonResponse<unknown>(contract.path, {
      body,
      headers,
      signal: options.signal,
    });
    const dataVersion = response.headers.get("X-Data-Version");
    const etag = response.headers.get("ETag");

    if (dataVersion === null || dataVersion.length === 0) {
      throw new ApiError(502, "stock-connect-data-version-missing");
    }
    if (etag === null || !/^"[A-Za-z0-9._:-]{1,158}"$/u.test(etag)) {
      throw new ApiError(502, "stock-connect-etag-invalid");
    }

    if (response.status === 204) {
      if (
        response.data !== undefined ||
        options.previous === undefined ||
        options.previous.dataVersion !== dataVersion ||
        options.previous.etag !== etag
      ) {
        throw new ApiError(502, "stock-connect-not-modified-without-cache");
      }

      return options.previous;
    }
    if (response.data === undefined) {
      throw new ApiError(502, "stock-connect-success-body-missing");
    }

    const parsedResponse = contract.responseSchema.safeParse(response.data);
    if (!parsedResponse.success) {
      throw new ApiError(502, "stock-connect-contract-invalid");
    }
    if (contract.dataVersion(parsedResponse.data) !== dataVersion) {
      throw new ApiError(502, "stock-connect-data-version-mismatch");
    }

    return {
      data: parsedResponse.data,
      dataVersion,
      etag,
    };
  });
}

/** 校验精确日期响应没有被服务端静默回退。 */
function responseMatchesDateSelection(
  resolvedTradeDate: string,
  date: StockConnectOverviewQuery["date"],
): boolean {
  return date.mode === "LATEST" || resolvedTradeDate === date.exactDate;
}

/** 校验总览返回的通道集合与请求集合完全一致。 */
function responseMatchesRequestedChannels(
  response: StockConnectOverviewResponse,
  request: StockConnectOverviewQuery,
): boolean {
  const requested = new Set(request.channels);
  const returned = new Set(
    response.channels.map(
      /** 只投影通道代码，以便同时检查缺失项与重复项。 */
      (summary) => summary.channel,
    ),
  );
  return (
    response.channels.length === requested.size &&
    returned.size === response.channels.length &&
    request.channels.every(
      /** 每个请求通道必须在共同 publication 中恰好出现一次。 */
      (channel) => returned.has(channel),
    ) &&
    response.trend.every(
      /** 每个趋势点都必须属于本次请求通道集合。 */
      (point) => requested.has(point.channel),
    )
  );
}

/** 校验历史行在同一交易日共享 bundle 版本，且当前交易日与外层 publication 一致。 */
function historyVersionsMatchPublication(
  rows: ReadonlyArray<{ tradeDate: string; dataVersion: string }>,
  publicationTradeDate: string,
  publicationDataVersion: string,
): boolean {
  const versionsByTradeDate = new Map<string, string>();

  return rows.every(
    /** 历史日期只能与同日行比较；当前解析日还必须锚定外层正式 publication。 */
    (row) => {
      const existingVersion = versionsByTradeDate.get(row.tradeDate);
      if (existingVersion !== undefined && existingVersion !== row.dataVersion) {
        return false;
      }
      versionsByTradeDate.set(row.tradeDate, row.dataVersion);

      return row.tradeDate !== publicationTradeDate || row.dataVersion === publicationDataVersion;
    },
  );
}

/** 查询四通道共同交易日总览及带通道身份的真实日终趋势。 */
export async function queryStockConnectOverview(
  request: StockConnectOverviewQuery,
  options: StockConnectReadOptions<StockConnectOverviewResponse> = {},
): Promise<VersionedStockConnectResponse<StockConnectOverviewResponse>> {
  const response = await readStockConnectResource(
    {
      path: "/api/v1/market/stock-connect/overview/query",
      requestSchema: stockConnectOverviewQuerySchema,
      responseSchema: stockConnectOverviewResponseSchema,
      dataVersion: (response) => response.publication.dataVersion,
    },
    request,
    options,
  );
  if (
    !responseMatchesDateSelection(response.data.resolvedTradeDate, request.date) ||
    response.data.publication.tradeDate !== response.data.resolvedTradeDate ||
    response.data.channels.some(
      /** 通道摘要必须属于共同 publication 的解析交易日。 */
      (summary) => summary.tradeDate !== response.data.resolvedTradeDate,
    ) ||
    !historyVersionsMatchPublication(
      response.data.trend,
      response.data.publication.tradeDate,
      response.data.publication.dataVersion,
    ) ||
    !responseMatchesRequestedChannels(response.data, request)
  ) {
    throw new ApiError(502, "stock-connect-response-scope-mismatch");
  }

  return response;
}

/** 查询持久化日历、预检、执行与 publication 证据形成的独立 readiness。 */
export async function queryStockConnectReadiness(
  request: StockConnectReadinessQuery,
  options: StockConnectReadOptions<StockConnectReadinessResponse> = {},
): Promise<VersionedStockConnectResponse<StockConnectReadinessResponse>> {
  const response = await readStockConnectResource(
    {
      path: "/api/v1/market/stock-connect/readiness/query",
      requestSchema: stockConnectReadinessQuerySchema,
      responseSchema: stockConnectReadinessResponseSchema,
      dataVersion: (body) => body.dataVersion,
    },
    request,
    options,
  );
  const requestedChannels = [...request.channels].sort();
  if (
    response.data.mode !== request.date.mode ||
    response.data.requestedExactDate !== request.date.exactDate ||
    response.data.selectedChannels.length !== requestedChannels.length ||
    response.data.selectedChannels.some(
      /** readiness 必须按稳定顺序精确回显请求通道。 */
      (channel, index) => channel !== requestedChannels[index],
    ) ||
    response.data.channels.some(
      /** 逐通道状态矩阵不能缺项、重复或夹带其他通道。 */
      (item, index) => item.channel !== requestedChannels[index],
    ) ||
    (request.date.mode === "EXACT" &&
      response.data.candidateTradeDate !== null &&
      response.data.candidateTradeDate !== request.date.exactDate) ||
    (await readinessDataVersion(response.data)) !== response.dataVersion
  ) {
    throw new ApiError(502, "stock-connect-response-scope-mismatch");
  }

  return response;
}

/** 查询一条通道的精确日统计、日终状态、额度与历史趋势。 */
export async function queryStockConnectChannel(
  request: StockConnectChannelQuery,
  options: StockConnectReadOptions<StockConnectChannelResponse> = {},
): Promise<VersionedStockConnectResponse<StockConnectChannelResponse>> {
  const response = await readStockConnectResource(
    {
      path: "/api/v1/market/stock-connect/channels/query",
      requestSchema: stockConnectChannelQuerySchema,
      responseSchema: stockConnectChannelResponseSchema,
      dataVersion: (response) => response.publication.dataVersion,
    },
    request,
    options,
  );
  if (
    !responseMatchesDateSelection(response.data.resolvedTradeDate, request.date) ||
    response.data.publication.tradeDate !== response.data.resolvedTradeDate ||
    response.data.channel.tradeDate !== response.data.resolvedTradeDate ||
    response.data.channel.channel !== request.channel ||
    !historyVersionsMatchPublication(
      response.data.trend,
      response.data.publication.tradeDate,
      response.data.publication.dataVersion,
    ) ||
    response.data.trend.some(
      /** 单通道接口不得夹带其他通道趋势。 */
      (point) => point.channel !== request.channel,
    )
  ) {
    throw new ApiError(502, "stock-connect-response-scope-mismatch");
  }

  return response;
}

/** 重算 readiness 规范 JSON 的 SHA-256，确认正文与服务端表示版本完全一致。 */
async function readinessDataVersion(response: StockConnectReadinessResponse): Promise<string> {
  const versionInput: Record<string, unknown> = { ...response };
  delete versionInput.dataVersion;
  const digest = await globalThis.crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(canonicalReadinessJson(versionInput)),
  );
  return [...new Uint8Array(digest)]
    .map(
      /** 输出合同要求的小写双位十六进制。 */
      (value) => value.toString(16).padStart(2, "0"),
    )
    .join("");
}

/** 递归按字典序序列化 JSON 对象并保留数组顺序、null 与 Unicode 原字符。 */
function canonicalReadinessJson(value: unknown): string {
  if (value === null || typeof value !== "object") {
    const encoded = JSON.stringify(value);
    if (encoded === undefined) throw new TypeError("readiness 含非 JSON 值。");
    return encoded;
  }
  if (Array.isArray(value)) return `[${value.map(canonicalReadinessJson).join(",")}]`;
  const object = value as Record<string, unknown>;
  return `{${Object.keys(object)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalReadinessJson(object[key])}`)
    .join(",")}}`;
}

/** 查询官方来源活跃证券榜及可用的榜内净额排序。 */
export async function queryStockConnectActiveSecurities(
  request: StockConnectActiveSecurityQuery,
  options: StockConnectReadOptions<StockConnectActiveSecurityPage> = {},
): Promise<VersionedStockConnectResponse<StockConnectActiveSecurityPage>> {
  const response = await readStockConnectResource(
    {
      path: "/api/v1/market/stock-connect/active-securities/query",
      requestSchema: stockConnectActiveSecurityQuerySchema,
      responseSchema: stockConnectActiveSecurityPageSchema,
      dataVersion: (response) => response.publication.dataVersion,
    },
    request,
    options,
  );
  const isSourceRanking = request.ranking === "SOURCE_ACTIVE";
  const rankingUnavailable = isSourceRanking
    ? response.data.rankingAvailability !== "REPORTED"
    : response.data.rankingAvailability !== "DERIVED";
  const netRankingHasUnavailableFacts =
    !isSourceRanking &&
    !rankingUnavailable &&
    response.data.items.some(
      /** 净额榜的每条记录都必须携带真实净额，不允许成交额替代。 */
      (item) => item.netBuyAmount.value === null,
    );
  // 即使服务端合同发生漂移，浏览器也不能把其他父 publication 的榜单拼入当前统计。
  const parentPublicationMismatch =
    response.data.publication.dataVersion !== request.parentPublicationDataVersion;
  if (
    !responseMatchesDateSelection(response.data.resolvedTradeDate, request.date) ||
    response.data.publication.tradeDate !== response.data.resolvedTradeDate ||
    parentPublicationMismatch ||
    response.data.channel !== request.channel ||
    response.data.ranking !== request.ranking ||
    (isSourceRanking && response.data.rankingAvailability !== "REPORTED") ||
    response.data.items.length > request.limit ||
    netRankingHasUnavailableFacts ||
    (rankingUnavailable && (response.data.items.length > 0 || response.data.nextCursor !== null))
  ) {
    throw new ApiError(502, "stock-connect-response-scope-mismatch");
  }

  return response;
}

/** 查询一个稳定证券身份的互联互通出现记录，不扩展为完整港股详情。 */
export async function queryStockConnectSecurityContext(
  request: StockConnectSecurityContextQuery,
  options: StockConnectReadOptions<StockConnectSecurityContextResponse> = {},
): Promise<VersionedStockConnectResponse<StockConnectSecurityContextResponse>> {
  const response = await readStockConnectResource(
    {
      path: "/api/v1/market/stock-connect/securities/context/query",
      requestSchema: stockConnectSecurityContextQuerySchema,
      responseSchema: stockConnectSecurityContextResponseSchema,
      dataVersion: (response) => response.publication.dataVersion,
    },
    request,
    options,
  );
  if (
    !responseMatchesDateSelection(response.data.resolvedTradeDate, request.date) ||
    response.data.publication.tradeDate !== response.data.resolvedTradeDate ||
    response.data.identity.instrumentEntityRef !== request.instrumentEntityRef ||
    !historyVersionsMatchPublication(
      response.data.activities,
      response.data.publication.tradeDate,
      response.data.publication.dataVersion,
    ) ||
    (request.channel !== null &&
      response.data.activities.some(
        /** 指定通道时，活动记录不得夹带其他通道。 */
        (activity) => activity.channel !== request.channel,
      ))
  ) {
    throw new ApiError(502, "stock-connect-response-scope-mismatch");
  }

  return response;
}
