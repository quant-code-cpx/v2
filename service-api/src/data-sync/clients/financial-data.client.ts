import { HttpStatus, Injectable } from '@nestjs/common';
import { z, type ZodType } from 'zod';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { AppConfigService } from '../../config/app-config.service.js';
import {
  financialMetricPageSchema,
  financialReportDetailSchema,
  financialReportPageSchema,
  internalFinancialMetricPageSchema,
  internalFinancialReportDetailSchema,
  internalFinancialReportPageSchema,
  internalValuationPageSchema,
  valuationPageSchema,
  type FinancialMetricPage,
  type FinancialReportDetail,
  type FinancialReportPage,
  type InternalFinancialMetricPage,
  type InternalFinancialReportDetail,
  type InternalFinancialReportPage,
  type InternalValuationPage,
  type ValuationPage,
} from '../contracts/financial-data.contract.js';

/** 描述可由测试替换的标准 Fetch 传输。 */
type FetchLike = typeof fetch;

/** 约束所有财务成功响应都携带消费者发布版本。 */
type VersionedBody = { dataVersion: string };

/** 描述内部 304 与公开 POST 204 映射所需的条件读取元数据。 */
export type FinancialConditionalRead<T extends VersionedBody> =
  | { status: 304; etag: string; dataVersion: string }
  | { status: 200; etag: string; dataVersion: string; body: T };

/** 通过内部 0013 契约读取财务数据，并裁剪内部身份与来源血缘。 */
@Injectable()
export class FinancialDataClient {
  /** 使用集中配置和可替换 `fetch` 构造防腐边界。 */
  public constructor(
    private readonly config: AppConfigService,
    private readonly fetcher: FetchLike = fetch,
  ) {}

  /** 读取一页已发布财务报表头。 */
  public listReports(input: {
    exchange: string;
    symbol: string;
    dataVersion: string;
    statementTypes?: readonly string[] | undefined;
    periodBases?: readonly string[] | undefined;
    scope?: string | undefined;
    methodologyCode: string;
    methodologyVersion: number;
    reportPeriodFrom?: string | undefined;
    reportPeriodTo?: string | undefined;
    asOf?: string | undefined;
    knownAt?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<FinancialConditionalRead<FinancialReportPage>> {
    const parameters = methodologyParameters(input);
    appendMany(parameters, 'statementType', input.statementTypes);
    appendMany(parameters, 'basis', input.periodBases);
    setOptional(parameters, 'scope', input.scope);
    setOptional(parameters, 'reportPeriodFrom', input.reportPeriodFrom);
    setOptional(parameters, 'reportPeriodTo', input.reportPeriodTo);
    setTemporalPageParameters(parameters, input);
    return this.request(
      financialPath(input.exchange, input.symbol, `financial-reports?${parameters.toString()}`),
      input.ifNoneMatch,
      input.requestId,
      internalFinancialReportPageSchema,
      publicFinancialReportPage,
    );
  }

  /** 读取一份已发布报表的治理行项目页。 */
  public getReport(input: {
    exchange: string;
    symbol: string;
    dataVersion: string;
    reportRef: string;
    metrics?: readonly string[] | undefined;
    asOf?: string | undefined;
    knownAt?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<FinancialConditionalRead<FinancialReportDetail>> {
    const parameters = new URLSearchParams({ limit: String(input.limit) });
    appendMany(parameters, 'metric', input.metrics);
    setTemporalPageParameters(parameters, input);
    return this.request(
      financialPath(
        input.exchange,
        input.symbol,
        `financial-reports/${encodeURIComponent(input.reportRef)}?${parameters.toString()}`,
      ),
      input.ifNoneMatch,
      input.requestId,
      internalFinancialReportDetailSchema,
      publicFinancialReportDetail,
    );
  }

  /** 读取一个显式来源和方法学的财务指标页。 */
  public listMetrics(input: {
    exchange: string;
    symbol: string;
    dataVersion: string;
    origin: string;
    methodologyCode: string;
    methodologyVersion: number;
    metrics: readonly string[];
    periodBases?: readonly string[] | undefined;
    reportPeriodFrom?: string | undefined;
    reportPeriodTo?: string | undefined;
    asOf?: string | undefined;
    knownAt?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<FinancialConditionalRead<FinancialMetricPage>> {
    const parameters = methodologyParameters(input);
    parameters.set('origin', input.origin);
    appendMany(parameters, 'metric', input.metrics);
    appendMany(parameters, 'basis', input.periodBases);
    setOptional(parameters, 'reportPeriodFrom', input.reportPeriodFrom);
    setOptional(parameters, 'reportPeriodTo', input.reportPeriodTo);
    setTemporalPageParameters(parameters, input);
    return this.request(
      financialPath(input.exchange, input.symbol, `financial-metrics?${parameters.toString()}`),
      input.ifNoneMatch,
      input.requestId,
      internalFinancialMetricPageSchema,
      publicFinancialMetricPage,
    );
  }

  /** 读取一个显式方法学和日期窗口的历史估值页。 */
  public listValuations(input: {
    exchange: string;
    symbol: string;
    dataVersion: string;
    methodologyCode: string;
    methodologyVersion: number;
    metrics: readonly string[];
    start: string;
    end: string;
    asOf?: string | undefined;
    knownAt?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<FinancialConditionalRead<ValuationPage>> {
    const parameters = methodologyParameters(input);
    appendMany(parameters, 'metric', input.metrics);
    parameters.set('start', input.start);
    parameters.set('end', input.end);
    setTemporalPageParameters(parameters, input);
    return this.request(
      financialPath(input.exchange, input.symbol, `valuations?${parameters.toString()}`),
      input.ifNoneMatch,
      input.requestId,
      internalValuationPageSchema,
      publicValuationPage,
    );
  }

  /** 发起幂等内部 GET，验证响应头、内部 schema 与公开裁剪结果。 */
  private async request<TInternal extends VersionedBody, TPublic extends VersionedBody>(
    requestPath: string,
    ifNoneMatch: string | undefined,
    requestId: string,
    internalSchema: ZodType<TInternal>,
    project: (input: TInternal) => TPublic,
  ): Promise<FinancialConditionalRead<TPublic>> {
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
    if (!response.ok) throw await upstreamProblem(response);
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

  /** 对连接错误、超时、502 或 503 的幂等内部 GET 最多安全重试一次。 */
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

/** 构造方法学和默认分页参数。 */
function methodologyParameters(input: {
  methodologyCode: string;
  methodologyVersion: number;
  limit: number;
}): URLSearchParams {
  return new URLSearchParams({
    methodologyCode: input.methodologyCode,
    methodologyVersion: String(input.methodologyVersion),
    limit: String(input.limit),
  });
}

/** 添加双时态、游标与页大小参数。 */
function setTemporalPageParameters(
  parameters: URLSearchParams,
  input: {
    dataVersion: string;
    asOf?: string | undefined;
    knownAt?: string | undefined;
    cursor?: string | undefined;
    limit: number;
  },
): void {
  parameters.set('dataVersion', input.dataVersion);
  setOptional(parameters, 'asOf', input.asOf);
  setOptional(parameters, 'knownAt', input.knownAt);
  setOptional(parameters, 'cursor', input.cursor);
  parameters.set('limit', String(input.limit));
}

/** 追加重复查询参数，保持调用方已经验证的顺序。 */
function appendMany(
  parameters: URLSearchParams,
  key: string,
  values: readonly string[] | undefined,
): void {
  for (const value of values ?? []) parameters.append(key, value);
}

/** 仅在值存在时添加查询参数。 */
function setOptional(parameters: URLSearchParams, key: string, value: string | undefined): void {
  if (value !== undefined) parameters.set(key, value);
}

/** 构造内部财务路径，并编码用户可控路径段。 */
function financialPath(exchange: string, symbol: string, suffix: string): string {
  return `/internal/v1/equities/${encodeURIComponent(exchange)}/${encodeURIComponent(symbol)}/${suffix}`;
}

/** 将内部报表页裁剪为公开 0014 合同。 */
function publicFinancialReportPage(input: InternalFinancialReportPage): FinancialReportPage {
  return financialReportPageSchema.parse({
    exchange: input.exchange,
    symbol: input.symbol,
    methodologyCode: input.methodologyCode,
    methodologyVersion: input.methodologyVersion,
    items: input.items.map(publicFinancialReportHeader),
    nextCursor: input.nextCursor,
    dataVersion: input.dataVersion,
    publishedAt: input.publishedAt,
    effectiveAsOf: input.effectiveAsOf,
    knowledgeCutoff: input.knowledgeCutoff,
  });
}

/** 将内部报表头裁剪为公开点时字段。 */
function publicFinancialReportHeader(
  input: InternalFinancialReportPage['items'][number],
): FinancialReportPage['items'][number] {
  return {
    reportRef: input.reportRef,
    exchange: input.exchange,
    symbol: input.symbol,
    statementType: input.statementType,
    reportPeriod: input.reportPeriod,
    periodBasis: input.periodBasis,
    statementScope: input.statementScope,
    currency: input.currency,
    currencyNullReason: input.currencyNullReason,
    reportType: input.reportType,
    auditStatus: input.auditStatus,
    announcementDate: input.announcementDate,
    providerUpdateDate: input.providerUpdateDate,
    availableFrom: input.effectiveFrom,
    knowledgeBasis: input.knowledgeBasis,
    knowledgeConfidence: input.knowledgeConfidence,
    revision: input.revision,
    methodologyCode: input.methodologyCode,
    methodologyVersion: input.methodologyVersion,
    qualityStatus: input.qualityStatus,
  };
}

/** 将内部报表详情裁剪为公开报表与治理字段。 */
function publicFinancialReportDetail(input: InternalFinancialReportDetail): FinancialReportDetail {
  return financialReportDetailSchema.parse({
    report: publicFinancialReportHeader(input.report),
    items: input.items.map(publicFinancialStatementItem),
    nextCursor: input.nextCursor,
    dataVersion: input.dataVersion,
    publishedAt: input.publishedAt,
    effectiveAsOf: input.effectiveAsOf,
    knowledgeCutoff: input.knowledgeCutoff,
  });
}

/** 将内部行项目裁剪为公开精确值与规范单位。 */
function publicFinancialStatementItem(
  input: InternalFinancialReportDetail['items'][number],
): FinancialReportDetail['items'][number] {
  return {
    metricCode: input.metricCode,
    label: input.label,
    value: input.value,
    nullReason: input.nullReason,
    currency: input.currency,
    currencyNullReason: input.currencyNullReason,
    unit: input.canonicalUnit,
  };
}

/** 将内部指标页裁剪为公开来源隔离序列。 */
function publicFinancialMetricPage(input: InternalFinancialMetricPage): FinancialMetricPage {
  return financialMetricPageSchema.parse({
    exchange: input.exchange,
    symbol: input.symbol,
    origin: input.origin,
    methodologyCode: input.methodologyCode,
    methodologyVersion: input.methodologyVersion,
    items: input.items.map(publicFinancialMetric),
    nextCursor: input.nextCursor,
    dataVersion: input.dataVersion,
    publishedAt: input.publishedAt,
    effectiveAsOf: input.effectiveAsOf,
    knowledgeCutoff: input.knowledgeCutoff,
  });
}

/** 将内部指标裁剪为公开点时与公式版本字段。 */
function publicFinancialMetric(
  input: InternalFinancialMetricPage['items'][number],
): FinancialMetricPage['items'][number] {
  return {
    metricCode: input.metricCode,
    label: input.label,
    origin: input.origin,
    reportPeriod: input.reportPeriod,
    periodBasis: input.periodBasis,
    statementScope: input.statementScope,
    value: input.value,
    unit: input.unit,
    currency: input.currency,
    currencyNullReason: input.currencyNullReason,
    methodologyCode: input.methodologyCode,
    methodologyVersion: input.methodologyVersion,
    formulaVersion: input.formulaVersion,
    availableFrom: input.effectiveFrom,
    knowledgeBasis: input.knowledgeBasis,
    knowledgeConfidence: input.knowledgeConfidence,
    revision: input.revision,
  };
}

/** 将内部估值页裁剪为公开非最终观察序列。 */
function publicValuationPage(input: InternalValuationPage): ValuationPage {
  return valuationPageSchema.parse({
    exchange: input.exchange,
    symbol: input.symbol,
    methodologyCode: input.methodologyCode,
    methodologyVersion: input.methodologyVersion,
    items: input.items.map(publicValuationObservation),
    nextCursor: input.nextCursor,
    dataVersion: input.dataVersion,
    publishedAt: input.publishedAt,
    effectiveAsOf: input.effectiveAsOf,
    knowledgeCutoff: input.knowledgeCutoff,
  });
}

/** 将内部估值观察裁剪为公开方法学与可见时间字段。 */
function publicValuationObservation(
  input: InternalValuationPage['items'][number],
): ValuationPage['items'][number] {
  return {
    observationDate: input.observationDate,
    metricCode: input.metricCode,
    value: input.value,
    unit: input.unit,
    currency: input.currency,
    currencyNullReason: input.currencyNullReason,
    methodologyCode: input.methodologyCode,
    methodologyVersion: input.methodologyVersion,
    finality: input.finality,
    availableFrom: input.effectiveFrom,
    knowledgeBasis: input.knowledgeBasis,
    knowledgeConfidence: input.knowledgeConfidence,
    revision: input.revision,
  };
}

/** 校验强 ETag 头存在且长度受控。 */
function validEtag(value: string | null): value is string {
  return value !== null && /^"[^"\r\n]{1,252}"$/.test(value);
}

/** 校验下游响应头中的发布 UUID。 */
function validDataVersion(value: string | null): value is string {
  return value !== null && z.string().uuid().safeParse(value).success;
}

/** 将内部依赖、鉴权、合同漂移和重试耗尽统一映射为公开 503。 */
function dependencyUnavailable(retryAfter?: number): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.SERVICE_UNAVAILABLE,
    'dependency-unavailable',
    'Financial data is temporarily unavailable',
    retryAfter,
  );
}

/** 将允许公开的内部状态映射为稳定 Problem Details，不透传下游正文。 */
async function upstreamProblem(response: Response): Promise<PublicProblemException> {
  if (response.status === 400) {
    return new PublicProblemException(
      HttpStatus.BAD_REQUEST,
      'validation-error',
      'Financial data query is invalid',
    );
  }
  if (response.status === 404) {
    return new PublicProblemException(
      HttpStatus.NOT_FOUND,
      'not-found',
      'Published financial data is not found',
    );
  }
  if (response.status === 409) {
    let code = 'cursor-mismatch';
    try {
      const body = z
        .object({ code: z.string() })
        .passthrough()
        .parse(await response.json());
      if (body.code === 'snapshot-expired') code = 'snapshot-expired';
    } catch {
      // 下游错误正文不是可信合同；未知 409 保持既有游标冲突语义。
    }
    return new PublicProblemException(
      HttpStatus.CONFLICT,
      code,
      code === 'snapshot-expired'
        ? 'Published financial snapshot changed'
        : 'Financial cursor does not match the requested snapshot',
    );
  }
  if (response.status === 429) {
    return new PublicProblemException(
      HttpStatus.TOO_MANY_REQUESTS,
      'rate-limited',
      'Financial data is rate limited',
      retryAfterSeconds(response),
    );
  }
  if (response.status === 503) return dependencyUnavailable(retryAfterSeconds(response));
  return dependencyUnavailable();
}

/** 从下游读取正整数秒数，拒绝无界、日期格式或畸形 Retry-After。 */
function retryAfterSeconds(response: Response): number | undefined {
  const value = Number(response.headers.get('retry-after'));
  return Number.isSafeInteger(value) && value > 0 && value <= 3600 ? value : undefined;
}
