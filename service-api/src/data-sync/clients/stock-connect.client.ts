import { HttpStatus, Injectable } from '@nestjs/common';
import { createHash } from 'node:crypto';
import { setTimeout as sleep } from 'node:timers/promises';
import type { ZodType } from 'zod';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { AppConfigService } from '../../config/app-config.service.js';
import {
  internalStockConnectProblemSchema,
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
  type StockConnectActiveSecurityPage,
  type StockConnectActiveSecurityQuery,
  type StockConnectChannelQuery,
  type StockConnectChannelResponse,
  type StockConnectOverviewQuery,
  type StockConnectOverviewResponse,
  type StockConnectReadinessQuery,
  type StockConnectReadinessResponse,
  type StockConnectSecurityContextQuery,
  type StockConnectSecurityContextResponse,
} from '../contracts/stock-connect.contract.js';

/** 表示可由单元测试替换的标准 Fetch 实现。 */
type FetchLike = typeof fetch;

/** 表示可替换的单调任务时钟，单位为 Unix 毫秒。 */
type Clock = () => number;

/** 表示可替换的有界重试等待实现。 */
type Sleeper = (milliseconds: number) => Promise<void>;

/** 表示可替换的随机数来源，仅用于 100–250ms 重试抖动。 */
type RandomSource = () => number;

/** 表示一次已严格校验的同步服务响应及其不可变数据版本。 */
export type StockConnectClientRead<T> = {
  body: T;
  dataVersion: string;
};

/** 表示下游 HTTP 错误是否应计入断路器。 */
type MappedProblem = {
  problem: PublicProblemException;
  dependencyFailure: boolean;
};

/** 描述成功响应必须与哪一种已校验请求保持业务范围一致。 */
type StockConnectRequestExpectation =
  | { operation: 'OVERVIEW'; request: StockConnectOverviewQuery }
  | { operation: 'READINESS'; request: StockConnectReadinessQuery }
  | { operation: 'CHANNEL'; request: StockConnectChannelQuery }
  | { operation: 'ACTIVE_SECURITIES'; request: StockConnectActiveSecurityQuery }
  | { operation: 'SECURITY_CONTEXT'; request: StockConnectSecurityContextQuery };

/** 汇总四类成功响应，供请求关联校验在严格 schema 之后安全分派。 */
type StockConnectResponse =
  | StockConnectOverviewResponse
  | StockConnectReadinessResponse
  | StockConnectChannelResponse
  | StockConnectActiveSecurityPage
  | StockConnectSecurityContextResponse;

/** 固定每条业务通道的方向、路由和业务金额原币。 */
const STOCK_CONNECT_CHANNEL_SEMANTICS = {
  SH_NORTHBOUND: { direction: 'NORTHBOUND', route: 'SHANGHAI', currency: 'CNY' },
  SZ_NORTHBOUND: { direction: 'NORTHBOUND', route: 'SHENZHEN', currency: 'CNY' },
  SH_SOUTHBOUND: { direction: 'SOUTHBOUND', route: 'SHANGHAI', currency: 'HKD' },
  SZ_SOUTHBOUND: { direction: 'SOUTHBOUND', route: 'SHENZHEN', currency: 'HKD' },
} as const satisfies Record<
  StockConnectChannelQuery['channel'],
  {
    direction: StockConnectChannelResponse['channel']['direction'];
    route: StockConnectChannelResponse['channel']['route'];
    currency: 'CNY' | 'HKD';
  }
>;

/** 互联互通各接口允许的最大成功响应字节数。 */
export const STOCK_CONNECT_RESPONSE_BYTES = {
  overview: 2 * 1024 * 1024,
  readiness: 64 * 1024,
  channel: 1024 * 1024,
  activeSecurities: 512 * 1024,
  securityContext: 1024 * 1024,
} as const;

/** 通过版本化内部 POST 合同读取真实互联互通 publication。 */
@Injectable()
export class StockConnectClient {
  /** 保存当前连续故障窗口内每次逻辑请求的时间。 */
  private readonly failureTimestamps: number[] = [];

  /** 保存断路器打开截止时间；零表示当前闭合。 */
  private circuitOpenUntil = 0;

  /** 标记冷却结束后唯一允许的半开探测，避免恢复瞬间的请求突刺。 */
  private halfOpenProbeInFlight = false;

  /** 注入集中配置以及可替换的网络、时钟和退避边界。 */
  public constructor(
    private readonly config: AppConfigService,
    private readonly fetcher: FetchLike = fetch,
    private readonly clock: Clock = Date.now,
    private readonly sleeper: Sleeper = sleep,
    private readonly random: RandomSource = Math.random,
  ) {}

  /** 查询所选通道最后一个共同完成 publication 的总览。 */
  public overview(
    input: unknown,
    requestId: string,
  ): Promise<StockConnectClientRead<StockConnectOverviewResponse>> {
    const request = parseRequest(stockConnectOverviewQuerySchema, input);
    return this.request(
      '/internal/v1/stock-connect/overview/query',
      request,
      requestId,
      stockConnectOverviewResponseSchema,
      STOCK_CONNECT_RESPONSE_BYTES.overview,
      { operation: 'OVERVIEW', request },
    );
  }

  /** 查询从持久化日历、预检、执行与 publication 证据生成的独立 readiness。 */
  public readiness(
    input: unknown,
    requestId: string,
  ): Promise<StockConnectClientRead<StockConnectReadinessResponse>> {
    const request = parseRequest(stockConnectReadinessQuerySchema, input);
    return this.request(
      '/internal/v1/stock-connect/readiness/query',
      request,
      requestId,
      stockConnectReadinessResponseSchema,
      STOCK_CONNECT_RESPONSE_BYTES.readiness,
      { operation: 'READINESS', request },
    );
  }

  /** 查询单条通道的日终统计、额度、状态和历史趋势。 */
  public channel(
    input: unknown,
    requestId: string,
  ): Promise<StockConnectClientRead<StockConnectChannelResponse>> {
    const request = parseRequest(stockConnectChannelQuerySchema, input);
    return this.request(
      '/internal/v1/stock-connect/channels/query',
      request,
      requestId,
      stockConnectChannelResponseSchema,
      STOCK_CONNECT_RESPONSE_BYTES.channel,
      { operation: 'CHANNEL', request },
    );
  }

  /** 查询官方来源活跃证券榜或仅在该榜内的可用净额排序。 */
  public activeSecurities(
    input: unknown,
    requestId: string,
  ): Promise<StockConnectClientRead<StockConnectActiveSecurityPage>> {
    const request = parseRequest(stockConnectActiveSecurityQuerySchema, input);
    return this.request(
      '/internal/v1/stock-connect/active-securities/query',
      request,
      requestId,
      stockConnectActiveSecurityPageSchema,
      STOCK_CONNECT_RESPONSE_BYTES.activeSecurities,
      { operation: 'ACTIVE_SECURITIES', request },
    );
  }

  /** 查询稳定证券身份在互联互通范围内的历史上下文。 */
  public securityContext(
    input: unknown,
    requestId: string,
  ): Promise<StockConnectClientRead<StockConnectSecurityContextResponse>> {
    const request = parseRequest(stockConnectSecurityContextQuerySchema, input);
    return this.request(
      '/internal/v1/stock-connect/securities/context/query',
      request,
      requestId,
      stockConnectSecurityContextResponseSchema,
      STOCK_CONNECT_RESPONSE_BYTES.securityContext,
      { operation: 'SECURITY_CONTEXT', request },
    );
  }

  /** 执行一个有界、可重试、带严格响应合同的内部只读 POST。 */
  private async request<T extends StockConnectResponse>(
    requestPath: string,
    request: unknown,
    requestId: string,
    schema: ZodType<T>,
    maximumBytes: number,
    expectation: StockConnectRequestExpectation,
  ): Promise<StockConnectClientRead<T>> {
    this.enterCircuit();
    try {
      const response = await this.fetchWithSingleRetry(requestPath, request, requestId);
      if (!response.ok) {
        const mapped = await this.problemFrom(response, requestId);
        if (mapped.dependencyFailure) {
          this.recordFailure();
        } else {
          this.recordSuccess();
        }
        throw mapped.problem;
      }

      const responseRequestId = response.headers.get('x-request-id');
      const dataVersion = response.headers.get('x-data-version');
      const contentType = response.headers.get('content-type');
      if (
        responseRequestId !== requestId ||
        !validDataVersion(dataVersion) ||
        !isJsonContentType(contentType)
      ) {
        await discardResponseBody(response);
        throw new Error('invalid stock-connect response headers');
      }

      const body = schema.parse(await readJsonObject(response, maximumBytes));
      if (
        responseDataVersion(body) !== dataVersion ||
        (expectation.operation === 'READINESS' &&
          readinessDataVersion(body as StockConnectReadinessResponse) !== dataVersion) ||
        !responseMatchesExpectation(body, expectation)
      ) {
        throw new Error('stock-connect response does not match request');
      }
      this.recordSuccess();
      return { body, dataVersion };
    } catch (error: unknown) {
      if (error instanceof PublicProblemException) throw error;
      this.recordFailure();
      throw dependencyUnavailable();
    }
  }

  /** 在 3 秒总预算内对网络中断、502 或 503 最多安全重试一次。 */
  private async fetchWithSingleRetry(
    requestPath: string,
    request: unknown,
    requestId: string,
  ): Promise<Response> {
    const deadline = this.clock() + this.config.dataSyncStockConnectTimeoutMs;
    let lastError: unknown;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const remainingMs = deadline - this.clock();
      if (remainingMs <= 0) break;
      try {
        const response = await this.fetcher(
          new URL(requestPath, this.config.dataSyncStockConnectBaseUrl),
          {
            method: 'POST',
            headers: {
              Accept: 'application/json',
              Authorization: `Bearer ${this.config.dataSyncStockConnectBearerToken}`,
              'Content-Type': 'application/json',
              'X-Request-Id': requestId,
            },
            body: JSON.stringify(request),
            signal: AbortSignal.timeout(remainingMs),
          },
        );
        if (attempt === 0 && (response.status === 502 || response.status === 503)) {
          await discardResponseBody(response);
          await this.pauseBeforeRetry(deadline);
          continue;
        }
        return response;
      } catch (error: unknown) {
        lastError = error;
        if (attempt === 0) {
          await this.pauseBeforeRetry(deadline);
          continue;
        }
      }
    }
    throw lastError instanceof Error ? lastError : new Error('stock-connect request timed out');
  }

  /** 在剩余总预算内执行 100–250ms 抖动退避。 */
  private async pauseBeforeRetry(deadline: number): Promise<void> {
    const remainingMs = deadline - this.clock();
    if (remainingMs <= 1) return;
    const jitterMs = 100 + Math.floor(this.random() * 151);
    await this.sleeper(Math.min(jitterMs, remainingMs - 1));
  }

  /** 将内部 Problem 映射为公开稳定错误，不转发下游正文。 */
  private async problemFrom(response: Response, requestId: string): Promise<MappedProblem> {
    if (response.status === 502 || response.status === 503) {
      const retryAfter = retryAfterSeconds(response);
      await discardResponseBody(response);
      return {
        problem: dependencyUnavailable(retryAfter),
        dependencyFailure: true,
      };
    }

    const contentType = response.headers.get('content-type');
    if (!isProblemContentType(contentType)) {
      await discardResponseBody(response);
      return { problem: dependencyUnavailable(), dependencyFailure: true };
    }
    try {
      const problem = internalStockConnectProblemSchema.parse(
        await readJsonObject(response, 16 * 1024),
      );
      if (problem.status !== response.status || problem.requestId !== requestId) {
        return { problem: dependencyUnavailable(), dependencyFailure: true };
      }
      if (response.status === 400 && problem.code === 'VALIDATION_FAILED') {
        return {
          problem: new PublicProblemException(
            HttpStatus.BAD_REQUEST,
            'VALIDATION_FAILED',
            'Stock-connect query is invalid',
          ),
          dependencyFailure: false,
        };
      }
      if (
        response.status === 409 &&
        (problem.code === 'EXACT_DATE_NOT_PUBLISHED' ||
          problem.code === 'PUBLICATION_NOT_READY' ||
          problem.code === 'READINESS_NOT_OBSERVED' ||
          problem.code === 'CURSOR_VERSION_MISMATCH' ||
          problem.code === 'PARENT_PUBLICATION_MISMATCH')
      ) {
        return {
          problem: new PublicProblemException(
            HttpStatus.CONFLICT,
            problem.code,
            problem.code === 'EXACT_DATE_NOT_PUBLISHED'
              ? 'Requested stock-connect trade date is not published'
              : problem.code === 'PUBLICATION_NOT_READY'
                ? 'Stock-connect publication is not ready'
                : problem.code === 'READINESS_NOT_OBSERVED'
                  ? 'Stock-connect readiness has not been observed'
                  : problem.code === 'CURSOR_VERSION_MISMATCH'
                    ? 'Stock-connect cursor belongs to another publication'
                    : 'Stock-connect parent publication changed',
            retryAfterSeconds(response),
          ),
          dependencyFailure: false,
        };
      }
      if (response.status === 404 && problem.code === 'SECURITY_CONTEXT_NOT_FOUND') {
        return {
          problem: new PublicProblemException(
            HttpStatus.NOT_FOUND,
            'SECURITY_CONTEXT_NOT_FOUND',
            'Stock-connect security context is not found',
          ),
          dependencyFailure: false,
        };
      }
      if (response.status === 429) {
        return {
          problem: new PublicProblemException(
            HttpStatus.TOO_MANY_REQUESTS,
            'RATE_LIMITED',
            'Stock-connect query is rate limited',
            retryAfterSeconds(response),
          ),
          dependencyFailure: false,
        };
      }
    } catch {
      return { problem: dependencyUnavailable(), dependencyFailure: true };
    }
    return { problem: dependencyUnavailable(), dependencyFailure: true };
  }

  /** 在断路器打开时快速失败，冷却结束后只允许一个半开探测。 */
  private enterCircuit(): void {
    const now = this.clock();
    if (this.circuitOpenUntil === 0) return;
    if (now < this.circuitOpenUntil || this.halfOpenProbeInFlight) {
      const retryAfter = Math.max(1, Math.ceil((this.circuitOpenUntil - now) / 1000));
      throw dependencyUnavailable(retryAfter);
    }
    this.halfOpenProbeInFlight = true;
  }

  /** 成功响应或确定的业务错误会关闭断路器并清除连续故障。 */
  private recordSuccess(): void {
    this.halfOpenProbeInFlight = false;
    this.circuitOpenUntil = 0;
    this.failureTimestamps.length = 0;
  }

  /** 记录一次完整逻辑请求故障，并按配置的连续失败窗口打开断路器。 */
  private recordFailure(): void {
    const now = this.clock();
    if (this.halfOpenProbeInFlight) {
      this.halfOpenProbeInFlight = false;
      this.circuitOpenUntil = now + this.config.dataSyncStockConnectCircuitOpenMs;
      return;
    }
    const windowStart = now - this.config.dataSyncStockConnectCircuitWindowMs;
    while (this.failureTimestamps[0] !== undefined && this.failureTimestamps[0] < windowStart) {
      this.failureTimestamps.shift();
    }
    this.failureTimestamps.push(now);
    if (this.failureTimestamps.length >= this.config.dataSyncStockConnectCircuitFailures) {
      this.circuitOpenUntil = now + this.config.dataSyncStockConnectCircuitOpenMs;
    }
  }
}

/** 在严格字段合同之后校验响应确实属于本次请求，而非其他合法 publication。 */
function responseMatchesExpectation(
  response: StockConnectResponse,
  expectation: StockConnectRequestExpectation,
): boolean {
  switch (expectation.operation) {
    case 'OVERVIEW':
      return overviewMatchesRequest(response as StockConnectOverviewResponse, expectation.request);
    case 'READINESS':
      return readinessMatchesRequest(
        response as StockConnectReadinessResponse,
        expectation.request,
      );
    case 'CHANNEL':
      return channelMatchesRequest(response as StockConnectChannelResponse, expectation.request);
    case 'ACTIVE_SECURITIES':
      return activeSecuritiesMatchRequest(
        response as StockConnectActiveSecurityPage,
        expectation.request,
      );
    case 'SECURITY_CONTEXT':
      return securityContextMatchesRequest(
        response as StockConnectSecurityContextResponse,
        expectation.request,
      );
  }
}

/** 返回业务 publication 或独立 readiness 在正文中声明的不可变表示版本。 */
function responseDataVersion(response: StockConnectResponse): string {
  return 'schemaVersion' in response ? response.dataVersion : response.publication.dataVersion;
}

/** 重算 readiness 规范 JSON 的 SHA-256，防止正文与同步服务声明版本脱钩。 */
function readinessDataVersion(response: StockConnectReadinessResponse): string {
  const versionInput: Record<string, unknown> = { ...response };
  delete versionInput.dataVersion;
  return createHash('sha256').update(canonicalReadinessJson(versionInput), 'utf8').digest('hex');
}

/** 递归按字典序序列化 JSON 对象并保留数组顺序、null 与 Unicode 原字符。 */
function canonicalReadinessJson(value: unknown): string {
  if (value === null || typeof value !== 'object') {
    const encoded = JSON.stringify(value);
    if (encoded === undefined) throw new Error('readiness contains a non-JSON value');
    return encoded;
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalReadinessJson).join(',')}]`;
  }
  const object = value as Record<string, unknown>;
  return `{${Object.keys(object)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalReadinessJson(object[key])}`)
    .join(',')}}`;
}

/** 校验 readiness 模式、请求日期和稳定通道集合与本次查询完全一致。 */
function readinessMatchesRequest(
  response: StockConnectReadinessResponse,
  request: StockConnectReadinessQuery,
): boolean {
  const requestedChannels = [...request.channels].sort();
  if (
    response.mode !== request.date.mode ||
    response.requestedExactDate !== request.date.exactDate ||
    response.selectedChannels.length !== requestedChannels.length ||
    response.selectedChannels.some((channel, index) => channel !== requestedChannels[index]) ||
    response.channels.some((item, index) => item.channel !== requestedChannels[index])
  ) {
    return false;
  }
  return (
    request.date.mode === 'LATEST' ||
    response.candidateTradeDate === null ||
    response.candidateTradeDate === request.date.exactDate
  );
}

/** 校验总览的精确日期、通道集合、固定语义和逐日完整趋势矩阵。 */
function overviewMatchesRequest(
  response: StockConnectOverviewResponse,
  request: StockConnectOverviewQuery,
): boolean {
  if (
    !resolvedDateMatchesRequest(response.resolvedTradeDate, request.date) ||
    response.dateResolution !== (request.date.mode === 'EXACT' ? 'EXACT' : 'LATEST_COMMON') ||
    response.channels.length !== request.channels.length
  ) {
    return false;
  }

  const requestedChannels = new Set(request.channels);
  const returnedChannels = new Set<StockConnectChannelQuery['channel']>();
  for (const summary of response.channels) {
    if (
      !requestedChannels.has(summary.channel) ||
      returnedChannels.has(summary.channel) ||
      summary.tradeDate !== response.resolvedTradeDate ||
      !channelSummaryUsesFixedSemantics(summary)
    ) {
      return false;
    }
    returnedChannels.add(summary.channel);
  }
  if (returnedChannels.size !== requestedChannels.size) return false;

  const trendChannelsByDate = new Map<string, Map<StockConnectChannelQuery['channel'], string>>();
  for (const point of response.trend) {
    if (
      !requestedChannels.has(point.channel) ||
      point.tradeDate > response.resolvedTradeDate ||
      !marketStatsUseCurrency(point.stats, expectedCurrency(point.channel))
    ) {
      return false;
    }
    const dayChannels =
      trendChannelsByDate.get(point.tradeDate) ??
      new Map<StockConnectChannelQuery['channel'], string>();
    if (dayChannels.has(point.channel)) return false;
    const knownVersion = dayChannels.values().next().value;
    if (knownVersion !== undefined && knownVersion !== point.dataVersion) return false;
    dayChannels.set(point.channel, point.dataVersion);
    trendChannelsByDate.set(point.tradeDate, dayChannels);
  }

  if (
    trendChannelsByDate.size === 0 ||
    trendChannelsByDate.size > request.trendTradingDays ||
    !trendChannelsByDate.has(response.resolvedTradeDate)
  ) {
    return false;
  }
  for (const [tradeDate, dayChannels] of trendChannelsByDate) {
    if (dayChannels.size !== requestedChannels.size) return false;
    if (
      tradeDate === response.resolvedTradeDate &&
      [...dayChannels.values()].some(
        /** 当前解析日的趋势必须与根 publication 使用同一个不可变版本。 */
        (dataVersion) => dataVersion !== response.publication.dataVersion,
      )
    ) {
      return false;
    }
  }
  return true;
}

/** 校验通道详情只返回请求通道及其固定方向、路由和业务币种。 */
function channelMatchesRequest(
  response: StockConnectChannelResponse,
  request: StockConnectChannelQuery,
): boolean {
  if (
    !resolvedDateMatchesRequest(response.resolvedTradeDate, request.date) ||
    response.dateResolution !== (request.date.mode === 'EXACT' ? 'EXACT' : 'LATEST_CHANNEL') ||
    response.channel.channel !== request.channel ||
    response.channel.tradeDate !== response.resolvedTradeDate ||
    !channelSummaryUsesFixedSemantics(response.channel)
  ) {
    return false;
  }
  const trendVersionsByDate = new Map<string, string>();
  for (const point of response.trend) {
    if (
      point.channel !== request.channel ||
      point.tradeDate > response.resolvedTradeDate ||
      trendVersionsByDate.has(point.tradeDate) ||
      !marketStatsUseCurrency(point.stats, expectedCurrency(request.channel))
    ) {
      return false;
    }
    trendVersionsByDate.set(point.tradeDate, point.dataVersion);
  }
  if (
    trendVersionsByDate.size === 0 ||
    trendVersionsByDate.size > request.trendTradingDays ||
    trendVersionsByDate.get(response.resolvedTradeDate) !== response.publication.dataVersion
  ) {
    return false;
  }
  return true;
}

/** 校验榜单请求范围、父 publication、净额符号和通道业务原币。 */
function activeSecuritiesMatchRequest(
  response: StockConnectActiveSecurityPage,
  request: StockConnectActiveSecurityQuery,
): boolean {
  if (
    !resolvedDateMatchesRequest(response.resolvedTradeDate, request.date) ||
    !activeDateResolutionMatchesRequest(response.dateResolution, request.date) ||
    response.channel !== request.channel ||
    response.ranking !== request.ranking ||
    response.publication.dataVersion !== request.parentPublicationDataVersion ||
    response.items.length > request.limit
  ) {
    return false;
  }

  const currency = expectedCurrency(request.channel);
  for (const item of response.items) {
    if (
      !moneyFactUsesCurrency(item.buyAmount, currency) ||
      !moneyFactUsesCurrency(item.sellAmount, currency) ||
      !moneyFactUsesCurrency(item.turnoverAmount, currency) ||
      !moneyFactUsesCurrency(item.netBuyAmount, currency)
    ) {
      return false;
    }
    const netAmount = item.netBuyAmount.value?.amount;
    if (
      (request.ranking === 'NET_BUY' &&
        (netAmount === undefined || decimalSign(netAmount) !== 1)) ||
      (request.ranking === 'NET_SELL' && (netAmount === undefined || decimalSign(netAmount) !== -1))
    ) {
      return false;
    }
  }
  return true;
}

/** 校验证券上下文始终锚定请求实体，并按筛选通道保持历史业务原币一致。 */
function securityContextMatchesRequest(
  response: StockConnectSecurityContextResponse,
  request: StockConnectSecurityContextQuery,
): boolean {
  if (
    !resolvedDateMatchesRequest(response.resolvedTradeDate, request.date) ||
    response.identity.identityAvailability !== 'RESOLVED' ||
    response.identity.instrumentEntityRef !== request.instrumentEntityRef
  ) {
    return false;
  }
  const activityKeys = new Set<string>();
  const activityVersionByDate = new Map<string, string>();
  for (const activity of response.activities) {
    const currency = expectedCurrency(activity.channel);
    const activityKey = `${activity.tradeDate}\n${activity.channel}`;
    const knownDateVersion = activityVersionByDate.get(activity.tradeDate);
    if (
      (request.channel !== null && activity.channel !== request.channel) ||
      activity.tradeDate > response.resolvedTradeDate ||
      activityKeys.has(activityKey) ||
      (request.channel === null &&
        knownDateVersion !== undefined &&
        knownDateVersion !== activity.dataVersion) ||
      !moneyFactUsesCurrency(activity.turnoverAmount, currency) ||
      !moneyFactUsesCurrency(activity.netBuyAmount, currency)
    ) {
      return false;
    }
    activityKeys.add(activityKey);
    activityVersionByDate.set(activity.tradeDate, activity.dataVersion);
  }
  if (activityVersionByDate.size > request.historyTradingDays) {
    return false;
  }
  return true;
}

/** 校验活跃榜 latest 可继承总览共同日或单通道日，精确查询则禁止任何回退。 */
function activeDateResolutionMatchesRequest(
  resolution: StockConnectActiveSecurityPage['dateResolution'],
  selection: StockConnectActiveSecurityQuery['date'],
): boolean {
  return selection.mode === 'EXACT'
    ? resolution === 'EXACT'
    : resolution === 'LATEST_COMMON' || resolution === 'LATEST_CHANNEL';
}

/** 校验一个通道摘要采用固定方向、路由和原币，额度币种由 schema 独立固定为人民币。 */
function channelSummaryUsesFixedSemantics(
  summary: StockConnectChannelResponse['channel'],
): boolean {
  const semantics = STOCK_CONNECT_CHANNEL_SEMANTICS[summary.channel];
  return (
    summary.direction === semantics.direction &&
    summary.route === semantics.route &&
    marketStatsUseCurrency(summary.stats, semantics.currency)
  );
}

/** 校验市场统计中所有非空业务金额均采用该通道原币。 */
function marketStatsUseCurrency(
  stats: StockConnectChannelResponse['channel']['stats'],
  currency: 'CNY' | 'HKD',
): boolean {
  return (
    moneyFactUsesCurrency(stats.buyAmount, currency) &&
    moneyFactUsesCurrency(stats.sellAmount, currency) &&
    moneyFactUsesCurrency(stats.turnoverAmount, currency) &&
    moneyFactUsesCurrency(stats.netBuyAmount, currency) &&
    moneyFactUsesCurrency(stats.etfTurnoverAmount, currency)
  );
}

/** 校验可用金额的币种；不可用事实没有金额对象，因此不伪造币种。 */
function moneyFactUsesCurrency(
  fact: StockConnectChannelResponse['channel']['stats']['buyAmount'],
  currency: 'CNY' | 'HKD',
): boolean {
  return fact.value === null || fact.value.currency === currency;
}

/** 返回通道市场统计和证券榜必须使用的业务原币。 */
function expectedCurrency(channel: StockConnectChannelQuery['channel']): 'CNY' | 'HKD' {
  return STOCK_CONNECT_CHANNEL_SEMANTICS[channel].currency;
}

/** 比较精确日期请求；latest 只接受同步服务明确解析出的已发布交易日。 */
function resolvedDateMatchesRequest(
  resolvedTradeDate: string,
  selection: StockConnectOverviewQuery['date'],
): boolean {
  return selection.mode === 'LATEST' || resolvedTradeDate === selection.exactDate;
}

/** 不借助浮点数判断十进制定点字符串的正、负或零，避免大额精度损失。 */
function decimalSign(value: string): -1 | 0 | 1 {
  const negative = value.startsWith('-');
  const unsignedValue = negative ? value.slice(1) : value;
  if (!/[1-9]/.test(unsignedValue)) return 0;
  return negative ? -1 : 1;
}

/** 将服务内部调用错误收敛为公开且不泄露实现细节的 503。 */
function dependencyUnavailable(retryAfter?: number): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.SERVICE_UNAVAILABLE,
    'UPSTREAM_UNAVAILABLE',
    'Stock-connect data is temporarily unavailable',
    retryAfter,
  );
}

/** 在调用内部网络前再次执行严格请求合同，防止应用层漂移。 */
function parseRequest<T>(schema: ZodType<T>, input: unknown): T {
  const result = schema.safeParse(input);
  if (result.success) return result.data;
  throw new PublicProblemException(
    HttpStatus.BAD_REQUEST,
    'VALIDATION_FAILED',
    'Stock-connect query is invalid',
  );
}

/** 校验不可变 publication 版本头长度和控制字符边界。 */
function validDataVersion(value: string | null): value is string {
  return (
    value !== null && value.length >= 1 && value.length <= 160 && !containsControlCharacter(value)
  );
}

/** 检查版本头中会破坏 HTTP 头边界的 ASCII 控制字符。 */
function containsControlCharacter(value: string): boolean {
  for (const character of value) {
    const codePoint = character.codePointAt(0);
    if (codePoint !== undefined && (codePoint <= 31 || codePoint === 127)) return true;
  }
  return false;
}

/** 判断成功响应使用 JSON 媒体类型。 */
function isJsonContentType(value: string | null): boolean {
  return value !== null && /^application\/json(?:\s*;|$)/i.test(value);
}

/** 判断失败响应使用冻结合同要求的 Problem Details 媒体类型。 */
function isProblemContentType(value: string | null): boolean {
  return value !== null && /^application\/problem\+json(?:\s*;|$)/i.test(value);
}

/** 读取有字节上限的 JSON 对象，避免异常下游响应耗尽 Node 内存。 */
async function readJsonObject(
  response: Response,
  maximumBytes: number,
): Promise<Record<string, unknown>> {
  const declared = response.headers.get('content-length');
  if (declared !== null) {
    const declaredBytes = Number(declared);
    if (
      !/^[0-9]+$/.test(declared) ||
      !Number.isSafeInteger(declaredBytes) ||
      declaredBytes > maximumBytes
    ) {
      await discardResponseBody(response);
      throw new Error('stock-connect response has an invalid content length');
    }
  }

  const stream: unknown = response.body;
  if (!(stream instanceof ReadableStream)) {
    throw new Error('stock-connect response body is missing');
  }
  const reader = (stream as ReadableStream<Uint8Array>).getReader();
  const chunks: Uint8Array[] = [];
  let totalBytes = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value === undefined) continue;
      if (value.byteLength > maximumBytes - totalBytes) {
        await cancelStreamReader(reader);
        throw new Error('stock-connect response exceeds byte budget');
      }
      chunks.push(value);
      totalBytes += value.byteLength;
    }
  } finally {
    reader.releaseLock();
  }

  const bytes = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  const value: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('stock-connect response is not an object');
  }
  return value as Record<string, unknown>;
}

/** 在流仍被 reader 锁定时主动取消剩余字节，且不让取消异常覆盖原始越界错误。 */
async function cancelStreamReader(reader: ReadableStreamDefaultReader<Uint8Array>): Promise<void> {
  try {
    await reader.cancel();
  } catch {
    // 流已经异常关闭时，调用方仍需按原始合同错误失败关闭。
  }
}

/** 读取 1–3600 秒范围内的安全重试提示。 */
function retryAfterSeconds(response: Response): number | undefined {
  const value = Number(response.headers.get('retry-after'));
  return Number.isSafeInteger(value) && value >= 1 && value <= 3600 ? value : undefined;
}

/** 在重试前主动取消未消费响应体，及时归还底层连接资源。 */
async function discardResponseBody(response: Response): Promise<void> {
  try {
    await response.body?.cancel();
  } catch {
    // 连接已经关闭时无需覆盖原始可重试状态。
  }
}
