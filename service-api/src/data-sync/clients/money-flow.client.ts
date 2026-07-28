import { HttpStatus, Injectable } from '@nestjs/common';
import { z, type ZodType } from 'zod';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { AppConfigService } from '../../config/app-config.service.js';
import {
  internalMoneyFlowDailyPageSchema,
  internalMoneyFlowMethodologyPageSchema,
  internalMoneyFlowRankingPageSchema,
  moneyFlowDailyPageSchema,
  moneyFlowMethodologyPageSchema,
  moneyFlowRankingPageSchema,
  type InternalMoneyFlowDailyPage,
  type InternalMoneyFlowMethodologyPage,
  type InternalMoneyFlowRankingPage,
  type MoneyFlowDailyPage,
  type MoneyFlowMethodologyPage,
  type MoneyFlowRankingPage,
} from '../contracts/money-flow.contract.js';

type FetchLike = typeof fetch;
type VersionedBody = { dataVersion: string };

/** 描述内部 304 与公开 POST 204 映射所需的条件读取元数据。 */
export type MoneyFlowConditionalRead<T extends VersionedBody> =
  | { status: 304; etag: string; dataVersion: string }
  | { status: 200; etag: string; dataVersion: string; body: T };

/** 通过 0015 内部 GET 读取资金流，并裁剪所有服务私有主键和 adapter 字段。 */
@Injectable()
export class MoneyFlowClient {
  /** 使用集中配置和可替换 fetch 构造防腐边界。 */
  public constructor(
    private readonly config: AppConfigService,
    private readonly fetcher: FetchLike = fetch,
  ) {}

  /** 读取 API 可见的方法学目录，默认只请求 validated 技术状态。 */
  public listMethodologies(input: {
    semanticFamily?: string | undefined;
    methodologyStatus?: string | undefined;
    scopeType?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<MoneyFlowConditionalRead<MoneyFlowMethodologyPage>> {
    const parameters = new URLSearchParams({
      methodologyStatus: input.methodologyStatus ?? 'validated',
      limit: String(input.limit),
    });
    setOptional(parameters, 'semanticFamily', input.semanticFamily);
    setOptional(parameters, 'scopeType', input.scopeType);
    setOptional(parameters, 'cursor', input.cursor);
    return this.request(
      `/internal/v1/money-flow/methodologies?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      internalMoneyFlowMethodologyPageSchema,
      publicMethodologyPage,
    );
  }

  /** 读取一个证券、板块或市场的来源日序列。 */
  public listDaily(input: {
    methodologyId: string;
    methodologyVersion: string;
    scopePath: string;
    bucket: string;
    start: string;
    end: string;
    knownAt?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<MoneyFlowConditionalRead<MoneyFlowDailyPage>> {
    const parameters = new URLSearchParams({
      methodologyVersion: input.methodologyVersion,
      bucket: input.bucket,
      start: input.start,
      end: input.end,
      limit: String(input.limit),
    });
    setOptional(parameters, 'knownAt', input.knownAt);
    setOptional(parameters, 'cursor', input.cursor);
    return this.request(
      `/internal/v1/money-flow/methodologies/${encodeURIComponent(
        input.methodologyId,
      )}/daily-series/${input.scopePath}?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      internalMoneyFlowDailyPageSchema,
      publicDailyPage,
    );
  }

  /** 读取 exact 或 latest 的不可变供应商排行。 */
  public listRanking(input: {
    methodologyId: string;
    methodologyVersion: string;
    scopeType: string;
    universe: string;
    windowType: string;
    windowSize: number;
    bucket: string;
    tradeDate?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<MoneyFlowConditionalRead<MoneyFlowRankingPage>> {
    const parameters = new URLSearchParams({
      methodologyVersion: input.methodologyVersion,
      scopeType: input.scopeType,
      universe: input.universe,
      windowType: input.windowType,
      windowSize: String(input.windowSize),
      bucket: input.bucket,
      limit: String(input.limit),
    });
    setOptional(parameters, 'tradeDate', input.tradeDate);
    setOptional(parameters, 'cursor', input.cursor);
    return this.request(
      `/internal/v1/money-flow/methodologies/${encodeURIComponent(
        input.methodologyId,
      )}/supplier-rankings?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      internalMoneyFlowRankingPageSchema,
      publicRankingPage,
    );
  }

  /** 发起幂等内部 GET，验证响应头、严格 schema 与公开投影。 */
  private async request<TInternal extends VersionedBody, TPublic extends VersionedBody>(
    requestPath: string,
    ifNoneMatch: string | undefined,
    requestId: string,
    internalSchema: ZodType<TInternal>,
    project: (input: TInternal) => TPublic,
  ): Promise<MoneyFlowConditionalRead<TPublic>> {
    const url = new URL(requestPath, this.config.dataSyncInternalBaseUrl);
    const headers: Record<string, string> = {
      Accept: 'application/json',
      Authorization: `Bearer ${this.config.dataSyncInternalBearerToken}`,
      'X-Request-Id': requestId,
    };
    if (ifNoneMatch !== undefined) headers['If-None-Match'] = ifNoneMatch;
    const response = await this.fetchWithSingleRetry(url, headers);
    const etag = response.headers.get('etag');
    const dataVersion = response.headers.get('x-data-version');
    if (response.status === 304) {
      if (!validEtag(etag) || !validDataVersion(dataVersion)) throw dependencyUnavailable();
      return { status: 304, etag, dataVersion };
    }
    if (!response.ok) throw upstreamProblem(response);
    try {
      const internalBody = internalSchema.parse(await response.json());
      const publicBody = project(internalBody);
      if (
        !validEtag(etag) ||
        !validDataVersion(dataVersion) ||
        dataVersion !== publicBody.dataVersion
      ) {
        throw dependencyUnavailable();
      }
      return { status: 200, etag, dataVersion, body: publicBody };
    } catch (error) {
      if (error instanceof PublicProblemException) throw error;
      throw dependencyUnavailable();
    }
  }

  /** 对网络错误、超时、502 或 503 的幂等内部 GET 最多安全重试一次。 */
  private async fetchWithSingleRetry(url: URL, headers: Record<string, string>): Promise<Response> {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        const response = await this.fetcher(url, {
          method: 'GET',
          headers,
          signal: AbortSignal.timeout(this.config.dataSyncInternalRequestTimeoutMs),
        });
        if (attempt === 0 && (response.status === 502 || response.status === 503)) continue;
        return response;
      } catch {
        if (attempt === 1) throw dependencyUnavailable();
      }
    }
    throw dependencyUnavailable();
  }
}

/** 将内部方法学目录裁剪为仅含 production-enabled 的公开定义。 */
function publicMethodologyPage(input: InternalMoneyFlowMethodologyPage): MoneyFlowMethodologyPage {
  const items = input.items
    .filter((item) => item.productionEnabled && item.methodologyStatus === 'validated')
    .map((item) => ({
      methodologyId: item.methodologyId,
      methodologyVersion: item.methodologyVersion,
      methodologyStatus: item.methodologyStatus,
      upstreamSource: item.upstreamSource,
      sourceDataset: item.sourceDataset,
      semanticFamily: item.semanticFamily,
      scopeTypes: item.scopeTypes,
      universeIds: item.universeIds,
      supportedWindows: item.supportedWindows,
      buckets: item.buckets.map((bucket) => ({
        ...bucket,
        definitionStatus:
          bucket.definitionStatus === 'documented' ? ('documented' as const) : ('unknown' as const),
      })),
      supportedMeasures: item.supportedMeasures,
      ratioDenominator: item.ratioDenominator,
      directionDefinition: item.directionDefinition,
      finality: item.finality,
      currency: item.currency,
      rawAmountUnit: item.rawAmountUnit,
      standardAmountUnit: item.standardAmountUnit,
      conversionVersion: item.conversionVersion,
      effectiveFrom: item.effectiveFrom,
      retiredAt: item.retiredAt,
    }));
  if (items.length === 0) throw dependencyUnavailable();
  return moneyFlowMethodologyPageSchema.parse({
    dataVersion: input.dataVersion,
    publishedAt: input.publishedAt,
    items,
    nextCursor: input.nextCursor,
  });
}

/** 裁剪内部 scope 主键，并保留来源日序列全部解释字段。 */
function publicDailyPage(input: InternalMoneyFlowDailyPage): MoneyFlowDailyPage {
  return moneyFlowDailyPageSchema.parse({
    methodologyId: input.methodologyId,
    methodologyVersion: input.methodologyVersion,
    upstreamSource: input.upstreamSource,
    sourceDataset: input.sourceDataset,
    semanticFamily: input.semanticFamily,
    scope: publicScope(input.scope),
    universe: input.universe,
    bucket: input.bucket,
    supportedMeasures: input.supportedMeasures,
    ratioDenominator: input.ratioDenominator,
    directionDefinition: input.directionDefinition,
    windowType: input.windowType,
    windowSize: input.windowSize,
    currency: input.currency,
    amountUnit: input.amountUnit,
    knownAtApplied: input.knownAtApplied,
    dataVersion: input.dataVersion,
    publishedAt: input.publishedAt,
    items: input.items,
    nextCursor: input.nextCursor,
  });
}

/** 裁剪内部排行快照和 scope 主键，不重算 supplierPosition。 */
function publicRankingPage(input: InternalMoneyFlowRankingPage): MoneyFlowRankingPage {
  return moneyFlowRankingPageSchema.parse({
    methodologyId: input.methodologyId,
    methodologyVersion: input.methodologyVersion,
    upstreamSource: input.upstreamSource,
    sourceDataset: input.sourceDataset,
    semanticFamily: input.semanticFamily,
    scopeType: input.scopeType,
    universe: input.universe,
    targetTradeDate: input.targetTradeDate,
    sourceCutoffAt: input.sourceCutoffAt,
    observedAt: input.observedAt,
    finality: input.finality,
    windowType: input.windowType,
    windowSize: input.windowSize,
    bucket: input.bucket,
    supportedMeasures: input.supportedMeasures,
    ratioDenominator: input.ratioDenominator,
    directionDefinition: input.directionDefinition,
    rankingBasis: input.rankingBasis,
    currency: input.currency,
    amountUnit: input.amountUnit,
    qualityStatus: input.qualityStatus,
    dataVersion: input.dataVersion,
    publishedAt: input.publishedAt,
    items: input.items.map((item) => ({
      supplierPosition: item.supplierPosition,
      scope: publicScope(item.scope),
      grossInflow: item.grossInflow,
      grossOutflow: item.grossOutflow,
      netAmount: item.netAmount,
      netRatio: item.netRatio,
    })),
    nextCursor: input.nextCursor,
  });
}

/** 移除内部证券、板块和序列主键，只保留公开 scope 身份。 */
function publicScope(
  scope:
    InternalMoneyFlowDailyPage['scope'] | InternalMoneyFlowRankingPage['items'][number]['scope'],
): Record<string, unknown> {
  if (scope.scopeType === 'equity') {
    return {
      scopeType: scope.scopeType,
      exchange: scope.exchange,
      symbol: scope.symbol,
      name: scope.name,
    };
  }
  if (scope.scopeType === 'sector') {
    return {
      scopeType: scope.scopeType,
      scheme: scope.scheme,
      sectorCode: scope.sectorCode,
      name: scope.name,
    };
  }
  return scope;
}

/** 仅在值存在时添加查询参数。 */
function setOptional(parameters: URLSearchParams, key: string, value: string | undefined): void {
  if (value !== undefined) parameters.set(key, value);
}

/** 校验强 ETag 头存在且长度受控。 */
function validEtag(value: string | null): value is string {
  return value !== null && /^"[^"\r\n]{1,252}"$/.test(value);
}

/** 校验下游响应头中的发布 UUID。 */
function validDataVersion(value: string | null): value is string {
  return value !== null && z.string().uuid().safeParse(value).success;
}

/** 将依赖不可用、无技术 publication 和契约漂移统一映射为公开 503。 */
function dependencyUnavailable(retryAfter?: number): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.SERVICE_UNAVAILABLE,
    'dependency-unavailable',
    'Money-flow data is temporarily unavailable',
    retryAfter,
  );
}

/** 映射允许公开的内部状态，不透传下游正文或内部标识。 */
function upstreamProblem(response: Response): PublicProblemException {
  if (response.status === 400) {
    return new PublicProblemException(
      HttpStatus.BAD_REQUEST,
      'validation-error',
      'Money-flow query is invalid',
    );
  }
  if (response.status === 404) {
    return new PublicProblemException(
      HttpStatus.NOT_FOUND,
      'not-found',
      'Published money-flow data is not found',
    );
  }
  if (response.status === 409) {
    return new PublicProblemException(
      HttpStatus.CONFLICT,
      'query-conflict',
      'Money-flow cursor or identity range conflicts with this query',
    );
  }
  if (response.status === 429) {
    return new PublicProblemException(
      HttpStatus.TOO_MANY_REQUESTS,
      'rate-limited',
      'Money-flow data is rate limited',
      retryAfterSeconds(response),
    );
  }
  if (response.status === 503) return dependencyUnavailable(retryAfterSeconds(response));
  return dependencyUnavailable();
}

/** 从下游读取有界正整数 Retry-After 秒数。 */
function retryAfterSeconds(response: Response): number | undefined {
  const value = Number(response.headers.get('retry-after'));
  return Number.isSafeInteger(value) && value > 0 && value <= 3600 ? value : undefined;
}
