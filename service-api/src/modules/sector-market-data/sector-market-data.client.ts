import { HttpStatus, Injectable } from '@nestjs/common';
import type { ZodType } from 'zod';

import { AppConfigService } from '../../platform/config/app-config.service.js';
import { PublicProblemException } from '../../platform/http/problem.exception.js';
import {
  internalSectorBarPageSchema,
  internalSectorPageSchema,
  type InternalSectorBarPage,
  type InternalSectorPage,
  type Sector,
  type SectorBarPage,
  type SectorPage,
} from './sector-market-data.contract.js';

type FetchLike = typeof fetch;

type NotModified = {
  status: 304;
  etag: string | undefined;
};

type UpstreamSuccess<T> = {
  status: 200;
  etag: string | undefined;
  body: T;
};

export type UpstreamResponse<T> = NotModified | UpstreamSuccess<T>;

/** 将 API 请求映射为同步服务内部已发布板块数据的受限读取。 */
@Injectable()
export class SectorMarketDataClient {
  /** 使用受校验配置和可替换 fetch 实现构造下游调用边界。 */
  public constructor(
    private readonly config: AppConfigService,
    private readonly fetcher: FetchLike = fetch,
  ) {}

  /** 读取一个分类体系的目录页，并保留可复验 ETag 状态。 */
  public listSectors(input: {
    scheme: string;
    query?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
  }): Promise<UpstreamResponse<SectorPage>> {
    const parameters = new URLSearchParams({ scheme: input.scheme, limit: String(input.limit) });
    if (input.query !== undefined) parameters.set('query', input.query);
    if (input.cursor !== undefined) parameters.set('cursor', input.cursor);
    return this.request(
      `/internal/v1/sectors?${parameters.toString()}`,
      input.ifNoneMatch,
      internalSectorPageSchema,
    ).then((response) =>
      response.status === 304 ? response : { ...response, body: publicSectorPage(response.body) },
    );
  }

  /** 读取一个板块的直接上游物理周期 K 线页，不从日线推导周线或月线。 */
  public listBars(input: {
    scheme: string;
    code: string;
    period: string;
    start: string;
    end: string;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
  }): Promise<UpstreamResponse<SectorBarPage>> {
    const parameters = new URLSearchParams({
      period: input.period,
      start: input.start,
      end: input.end,
      limit: String(input.limit),
    });
    if (input.cursor !== undefined) parameters.set('cursor', input.cursor);
    return this.request(
      `/internal/v1/sectors/${encodeURIComponent(input.scheme)}/${encodeURIComponent(input.code)}/bars?${parameters.toString()}`,
      input.ifNoneMatch,
      internalSectorBarPageSchema,
    ).then((response) =>
      response.status === 304
        ? response
        : { ...response, body: publicSectorBarPage(response.body) },
    );
  }

  /** 发起有认证、超时和严格合同校验的只读下游请求。 */
  private async request<T>(
    path: string,
    ifNoneMatch: string | undefined,
    schema: ZodType<T>,
  ): Promise<UpstreamResponse<T>> {
    const url = new URL(path, this.config.dataSyncInternalBaseUrl);
    const headers: Record<string, string> = {
      Accept: 'application/json',
      Authorization: `Bearer ${this.config.dataSyncInternalBearerToken}`,
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
    const etag = response.headers.get('etag') ?? undefined;
    if (response.status === 304) return { status: 304, etag };
    if (!response.ok) throw upstreamProblem(response.status, response.headers.get('retry-after'));
    try {
      return { status: 200, etag, body: schema.parse(await response.json()) };
    } catch {
      throw dependencyUnavailable();
    }
  }
}

/** 去除内部 UUID，构造公开目录页。 */
function publicSectorPage(input: InternalSectorPage): SectorPage {
  return {
    items: input.items.map(publicSector),
    nextCursor: input.nextCursor,
    dataVersion: input.dataVersion,
    publishedAt: input.publishedAt,
  };
}

/** 去除嵌套内部 UUID，构造公开 K 线页。 */
function publicSectorBarPage(input: InternalSectorBarPage): SectorBarPage {
  return { ...input, sector: publicSector(input.sector) };
}

/** 移除内部稳定 UUID，确保浏览器永远不会依赖同步服务身份主键。 */
function publicSector(input: InternalSectorPage['items'][number]): Sector {
  const sector = { ...input } as Record<string, string>;
  delete sector.sectorId;
  return sector as Sector;
}

/** 将下游不可用、鉴权异常或合同漂移统一为公开 503。 */
function dependencyUnavailable(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.SERVICE_UNAVAILABLE,
    'dependency-unavailable',
    'Sector market data is temporarily unavailable',
  );
}

/** 按已冻结的公共合同映射下游状态，绝不转发下游响应体。 */
function upstreamProblem(status: number, retryAfter: string | null): PublicProblemException {
  if (status === 404) {
    return new PublicProblemException(
      HttpStatus.NOT_FOUND,
      'not-found',
      'Sector market data is not found',
    );
  }
  if (status === 409) {
    return new PublicProblemException(
      HttpStatus.CONFLICT,
      'snapshot-expired',
      'Published snapshot changed',
    );
  }
  if (status === 429) {
    const retry = Number(retryAfter);
    return new PublicProblemException(
      HttpStatus.TOO_MANY_REQUESTS,
      'rate-limited',
      'Sector market data is rate limited',
      Number.isSafeInteger(retry) && retry > 0 ? retry : undefined,
    );
  }
  return dependencyUnavailable();
}
