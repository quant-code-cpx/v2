import { HttpStatus, Injectable } from '@nestjs/common';
import type { ZodType } from 'zod';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { AppConfigService } from '../../config/app-config.service.js';
import {
  swIndustryPageSchema,
  swIndustryResourceSchema,
  swIndustryValuationPageSchema,
  type SwIndustryPage,
  type SwIndustryResource,
  type SwIndustryValuationPage,
} from '../contracts/sw-sector.contract.js';

type FetchLike = typeof fetch;

/** 描述下游条件读取未修改响应。 */
type NotModified = {
  status: 304;
  etag: string;
  dataVersion: string;
};

/** 描述通过 Zod 合同校验的下游成功响应。 */
type UpstreamSuccess<T> = {
  status: 200;
  etag: string;
  dataVersion: string;
  body: T;
};

/** 表示 service-api 保留的下游条件读取状态。 */
export type SwUpstreamResponse<T> = NotModified | UpstreamSuccess<T>;

/** 将公开申万查询隔离到版本化同步服务内部 HTTP 契约。 */
@Injectable()
export class SwSectorClient {
  /** 注入受校验配置和可替换 fetch 实现。 */
  public constructor(
    private readonly config: AppConfigService,
    private readonly fetcher: FetchLike = fetch,
  ) {}

  /** 分页读取一个冻结 taxonomy 发布。 */
  public listIndustries(input: {
    snapshotDate?: string | undefined;
    level?: number | undefined;
    parentCode?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<SwUpstreamResponse<SwIndustryPage>> {
    const parameters = swParameters(input);
    return this.request(
      `/internal/v1/sw-industries?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      swIndustryPageSchema,
    );
  }

  /** 读取一个节点及冻结 dataVersion 中的完整父级闭包。 */
  public getIndustry(input: {
    code: string;
    snapshotDate?: string | undefined;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<SwUpstreamResponse<SwIndustryResource>> {
    const parameters = new URLSearchParams();
    if (input.snapshotDate !== undefined) parameters.set('snapshotDate', input.snapshotDate);
    const suffix = parameters.size === 0 ? '' : `?${parameters.toString()}`;
    return this.request(
      `/internal/v1/sw-industries/${encodeURIComponent(input.code)}${suffix}`,
      input.ifNoneMatch,
      input.requestId,
      swIndustryResourceSchema,
    );
  }

  /** 分页读取一个冻结日期的申万估值观察。 */
  public listValuations(input: {
    snapshotDate?: string | undefined;
    level?: number | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<SwUpstreamResponse<SwIndustryValuationPage>> {
    const parameters = swParameters(input);
    return this.request(
      `/internal/v1/sw-industries/valuations?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      swIndustryValuationPageSchema,
    );
  }

  /** 发起带服务认证、硬超时和严格响应 schema 校验的内部 GET。 */
  private async request<T>(
    path: string,
    ifNoneMatch: string | undefined,
    requestId: string,
    schema: ZodType<T>,
  ): Promise<SwUpstreamResponse<T>> {
    if (!validRequestId(requestId)) throw dependencyUnavailable();
    const headers: Record<string, string> = {
      Accept: 'application/json',
      Authorization: `Bearer ${this.config.dataSyncInternalBearerToken}`,
      'X-Request-Id': requestId,
    };
    if (ifNoneMatch !== undefined) headers['If-None-Match'] = ifNoneMatch;
    let response: Response;
    try {
      response = await this.fetcher(new URL(path, this.config.dataSyncInternalBaseUrl), {
        method: 'GET',
        headers,
        signal: AbortSignal.timeout(this.config.dataSyncInternalRequestTimeoutMs),
      });
    } catch {
      throw dependencyUnavailable();
    }
    const etag = response.headers.get('etag');
    const dataVersion = response.headers.get('x-data-version');
    if (response.headers.get('x-request-id') !== requestId) {
      await response.body?.cancel();
      throw dependencyUnavailable();
    }
    if (response.status === 304) {
      if (!validEtag(etag) || !validDataVersion(dataVersion)) throw dependencyUnavailable();
      return { status: 304, etag, dataVersion };
    }
    if (!response.ok) throw upstreamProblem(response.status, response.headers.get('retry-after'));
    try {
      const body = schema.parse(await response.json());
      const bodyDataVersion = swBodyDataVersion(body);
      if (!validEtag(etag) || !validDataVersion(dataVersion) || bodyDataVersion !== dataVersion) {
        throw dependencyUnavailable();
      }
      return { status: 200, etag, dataVersion, body };
    } catch {
      throw dependencyUnavailable();
    }
  }
}

/** 构造受控日期、层级、父级、游标和页大小查询参数。 */
function swParameters(input: {
  snapshotDate?: string | undefined;
  level?: number | undefined;
  parentCode?: string | undefined;
  cursor?: string | undefined;
  limit: number;
}): URLSearchParams {
  const parameters = new URLSearchParams({ limit: String(input.limit) });
  if (input.snapshotDate !== undefined) parameters.set('snapshotDate', input.snapshotDate);
  if (input.level !== undefined) parameters.set('level', String(input.level));
  if (input.parentCode !== undefined) parameters.set('parentCode', input.parentCode);
  if (input.cursor !== undefined) parameters.set('cursor', input.cursor);
  return parameters;
}

/** 从申万 taxonomy、详情或估值响应共有的 release 读取数据版本。 */
function swBodyDataVersion(value: unknown): string | undefined {
  if (
    typeof value !== 'object' ||
    value === null ||
    !('release' in value) ||
    typeof value.release !== 'object' ||
    value.release === null ||
    !('dataVersion' in value.release) ||
    typeof value.release.dataVersion !== 'string'
  ) {
    return undefined;
  }
  return value.release.dataVersion;
}

/** 校验内部申万读端返回不含换行的强 ETag。 */
function validEtag(value: string | null): value is string {
  return value !== null && /^"[^"\r\n]{1,252}"$/.test(value);
}

/** 校验内部申万读端 publication 使用 UUID。 */
function validDataVersion(value: string | null): value is string {
  return (
    value !== null &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)
  );
}

/** 校验关联标识采用 UUID 或受限稳定字符集，禁止换行和空白进入服务间请求头。 */
function validRequestId(value: string): boolean {
  return /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/.test(value);
}

/** 将依赖网络、认证或合同漂移统一映射为公开 503。 */
function dependencyUnavailable(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.SERVICE_UNAVAILABLE,
    'dependency-unavailable',
    'SW industry data is temporarily unavailable',
  );
}

/** 按冻结公开语义映射下游状态，绝不透传内部问题响应体。 */
function upstreamProblem(status: number, retryAfter: string | null): PublicProblemException {
  if (status === 404) {
    return new PublicProblemException(
      HttpStatus.NOT_FOUND,
      'not-found',
      'SW industry is not found',
    );
  }
  if (status === 409) {
    return new PublicProblemException(
      HttpStatus.CONFLICT,
      'snapshot-expired',
      'SW industry snapshot changed',
    );
  }
  if (status === 429) {
    const retry = Number(retryAfter);
    return new PublicProblemException(
      HttpStatus.TOO_MANY_REQUESTS,
      'rate-limited',
      'SW industry data is rate limited',
      Number.isSafeInteger(retry) && retry > 0 ? retry : undefined,
    );
  }
  return dependencyUnavailable();
}
