import { HttpStatus, Injectable } from '@nestjs/common';
import { z, type ZodType } from 'zod';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { AppConfigService } from '../../config/app-config.service.js';
import type { ConditionalRead } from './equity-instrument.client.js';
import {
  equityAdjustmentFactorPageSchema,
  equityBarPageSchema,
  equityCompanyProfileSchema,
  equityCorporateActionPageSchema,
  type EquityAdjustmentFactorPage,
  type EquityBarPage,
  type EquityCompanyProfile,
  type EquityCorporateActionPage,
} from '../contracts/equity-market-data.contract.js';

type FetchLike = typeof fetch;

const conflictCodeSchema = z
  .object({
    code: z.enum(['adjustment-unavailable', 'identity-boundary-conflict']),
  })
  .passthrough();

type PublicConflictCode =
  'adjustment-unavailable' | 'identity-resolution-conflict' | 'snapshot-expired';

/** 通过内部只读 HTTP 契约访问方案 0011 市场数据，不连接同步数据库。 */
@Injectable()
export class EquityMarketDataClient {
  /** 使用集中配置和可替换 `fetch` 构造防腐边界。 */
  public constructor(
    private readonly config: AppConfigService,
    private readonly fetcher: FetchLike = fetch,
  ) {}

  /** 读取日、周或月独立行情及可选查询时复权结果。 */
  public listBars(input: {
    exchange: string;
    symbol: string;
    dataVersion: string;
    factorDataVersion?: string | undefined;
    period: string;
    start: string;
    end: string;
    adjust: string;
    adjustAsOf?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<ConditionalRead<EquityBarPage>> {
    const parameters = new URLSearchParams({
      dataVersion: input.dataVersion,
      period: input.period,
      start: input.start,
      end: input.end,
      adjust: input.adjust,
      limit: String(input.limit),
    });
    setOptional(parameters, 'adjustAsOf', input.adjustAsOf);
    setOptional(parameters, 'factorDataVersion', input.factorDataVersion);
    setOptional(parameters, 'cursor', input.cursor);
    return this.request(
      path(input.exchange, input.symbol, `bars?${parameters.toString()}`),
      input.ifNoneMatch,
      input.requestId,
      equityBarPageSchema,
    );
  }

  /** 读取稀疏累计后复权因子序列。 */
  public listAdjustmentFactors(input: {
    exchange: string;
    symbol: string;
    dataVersion: string;
    start?: string | undefined;
    end: string;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<ConditionalRead<EquityAdjustmentFactorPage>> {
    const parameters = new URLSearchParams({
      dataVersion: input.dataVersion,
      end: input.end,
      limit: String(input.limit),
    });
    setOptional(parameters, 'start', input.start);
    setOptional(parameters, 'cursor', input.cursor);
    return this.request(
      path(input.exchange, input.symbol, `adjustment-factors?${parameters.toString()}`),
      input.ifNoneMatch,
      input.requestId,
      equityAdjustmentFactorPageSchema,
    );
  }

  /** 读取分红送转事件当前 revision。 */
  public listCorporateActions(input: {
    exchange: string;
    symbol: string;
    dataVersion: string;
    start?: string | undefined;
    end?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<ConditionalRead<EquityCorporateActionPage>> {
    const parameters = new URLSearchParams({
      dataVersion: input.dataVersion,
      limit: String(input.limit),
    });
    setOptional(parameters, 'start', input.start);
    setOptional(parameters, 'end', input.end);
    setOptional(parameters, 'cursor', input.cursor);
    return this.request(
      path(input.exchange, input.symbol, `corporate-actions?${parameters.toString()}`),
      input.ifNoneMatch,
      input.requestId,
      equityCorporateActionPageSchema,
    );
  }

  /** 读取当前公司概况。 */
  public getCompanyProfile(input: {
    exchange: string;
    symbol: string;
    dataVersion: string;
    asOf?: string | undefined;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<ConditionalRead<EquityCompanyProfile>> {
    const parameters = new URLSearchParams({ dataVersion: input.dataVersion });
    setOptional(parameters, 'asOf', input.asOf);
    const query = parameters.size === 0 ? '' : `?${parameters.toString()}`;
    return this.request(
      path(input.exchange, input.symbol, `company-profile${query}`),
      input.ifNoneMatch,
      input.requestId,
      equityCompanyProfileSchema,
    );
  }

  /** 发起带内部凭据、超时、关联标识与严格 schema 校验的 GET。 */
  private async request<T extends { dataVersion: string | null }>(
    requestPath: string,
    ifNoneMatch: string | undefined,
    requestId: string,
    schema: ZodType<T>,
  ): Promise<ConditionalRead<T>> {
    const url = new URL(requestPath, this.config.dataSyncInternalBaseUrl);
    const headers: Record<string, string> = {
      Accept: 'application/json',
      Authorization: `Bearer ${this.config.dataSyncInternalBearerToken}`,
      'X-Request-Id': requestId,
    };
    if (ifNoneMatch !== undefined) headers['If-None-Match'] = ifNoneMatch;
    let response: Response;
    try {
      response = await this.fetcher(url, {
        method: 'GET',
        headers,
        signal: AbortSignal.timeout(this.config.dataSyncInternalRequestTimeoutMs),
      });
    } catch {
      throw dependencyUnavailable();
    }
    const etag = response.headers.get('etag');
    const dataVersion = response.headers.get('x-data-version');
    if (response.status === 304) {
      if (!validEtag(etag) || !validDataVersion(dataVersion)) throw dependencyUnavailable();
      return { status: 304, etag, dataVersion };
    }
    if (!response.ok) throw await upstreamProblem(response);
    try {
      const body = schema.parse(await response.json());
      if (!validEtag(etag) || !validDataVersion(dataVersion) || dataVersion !== body.dataVersion) {
        throw dependencyUnavailable();
      }
      return { status: 200, etag, dataVersion, body };
    } catch (error) {
      if (error instanceof PublicProblemException) throw error;
      throw dependencyUnavailable();
    }
  }
}

/** 构造内部证券市场数据路径并编码用户可控分段。 */
function path(exchange: string, symbol: string, suffix: string): string {
  return `/internal/v1/equities/${encodeURIComponent(exchange)}/${encodeURIComponent(symbol)}/${suffix}`;
}

/** 仅在值存在时添加查询参数。 */
function setOptional(parameters: URLSearchParams, key: string, value: string | undefined): void {
  if (value !== undefined) parameters.set(key, value);
}

/** 校验下游只返回受控强 ETag，防止弱校验器或非法字符进入公开响应头。 */
function validEtag(value: string | null): value is string {
  return value !== null && /^"[^"\r\n]{1,252}"$/.test(value);
}

/** 校验内部 publication 版本是规范 UUID。 */
function validDataVersion(value: string | null): value is string {
  return value !== null && z.string().uuid().safeParse(value).success;
}

/** 将下游不可用、鉴权异常、超时或合同漂移统一映射为公开 503。 */
function dependencyUnavailable(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.SERVICE_UNAVAILABLE,
    'dependency-unavailable',
    'Equity market data is temporarily unavailable',
  );
}

/** 将内部问题映射为公开稳定状态，不转发下游详情。 */
async function upstreamProblem(response: Response): Promise<PublicProblemException> {
  if (response.status === 400) {
    return new PublicProblemException(
      HttpStatus.BAD_REQUEST,
      'validation-error',
      'Equity market data query is invalid',
    );
  }
  if (response.status === 404) {
    return new PublicProblemException(
      HttpStatus.NOT_FOUND,
      'not-found',
      'Equity market data is not found',
    );
  }
  if (response.status === 409) {
    const code = await conflictCode(response);
    const detail =
      code === 'adjustment-unavailable'
        ? 'Adjustment factors do not cover the requested range'
        : code === 'identity-resolution-conflict'
          ? 'Instrument identity is ambiguous for the requested date range'
          : 'Published equity market snapshot changed';
    return new PublicProblemException(HttpStatus.CONFLICT, code, detail);
  }
  if (response.status === 429) {
    const retryAfter = Number(response.headers.get('retry-after'));
    return new PublicProblemException(
      HttpStatus.TOO_MANY_REQUESTS,
      'rate-limited',
      'Equity market data is rate limited',
      Number.isSafeInteger(retryAfter) && retryAfter > 0 ? retryAfter : undefined,
    );
  }
  return dependencyUnavailable();
}

/** 将内部身份边界冲突收敛为公开身份冲突，其余未知 409 使用稳定快照冲突。 */
async function conflictCode(response: Response): Promise<PublicConflictCode> {
  try {
    const parsed = conflictCodeSchema.safeParse(await response.json());
    if (!parsed.success) return 'snapshot-expired';
    return parsed.data.code === 'identity-boundary-conflict'
      ? 'identity-resolution-conflict'
      : parsed.data.code;
  } catch {
    return 'snapshot-expired';
  }
}
