import { HttpStatus, Injectable } from '@nestjs/common';
import type { ZodType } from 'zod';

import { AppConfigService } from '../../config/app-config.service.js';
import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import {
  internalEquitySectorPageSchema,
  internalSectorBarPageSchema,
  internalSectorConstituentPageSchema,
  internalSectorEodPageSchema,
  internalSectorEodResourceSchema,
  internalSectorPageSchema,
  type EquitySectorPage,
  type InternalEquitySectorPage,
  type InternalSectorBarPage,
  type InternalSectorConstituentPage,
  type InternalSectorEodPage,
  type InternalSectorEodResource,
  type InternalSectorPage,
  type Sector,
  type SectorBarPage,
  type SectorConstituentPage,
  type SectorEodPage,
  type SectorEodResource,
  type SectorPage,
} from '../contracts/sector-market-data.contract.js';

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

  /** 读取一个已发布 EOD 横截面排行页，并让下游严格验证版本化快照契约。 */
  public listEodSnapshots(input: {
    scheme: string;
    asOf?: string | undefined;
    sort: string;
    order: string;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
  }): Promise<UpstreamResponse<SectorEodPage>> {
    const parameters = new URLSearchParams({
      scheme: input.scheme,
      sort: input.sort,
      order: input.order,
      limit: String(input.limit),
    });
    if (input.asOf !== undefined) parameters.set('asOf', input.asOf);
    if (input.cursor !== undefined) parameters.set('cursor', input.cursor);
    return this.request(
      `/internal/v1/sectors/eod-snapshots?${parameters.toString()}`,
      input.ifNoneMatch,
      internalSectorEodPageSchema,
    ).then((response) =>
      response.status === 304
        ? response
        : { ...response, body: publicSectorEodPage(response.body) },
    );
  }

  /** 读取一个板块在 latest 或精确交易日 EOD 快照中的报价，不回退旧日期。 */
  public getEodSnapshot(input: {
    scheme: string;
    code: string;
    asOf?: string | undefined;
    ifNoneMatch?: string | undefined;
  }): Promise<UpstreamResponse<SectorEodResource>> {
    const parameters = new URLSearchParams();
    if (input.asOf !== undefined) parameters.set('asOf', input.asOf);
    const suffix = input.asOf === undefined ? '' : `?${parameters.toString()}`;
    return this.request(
      `/internal/v1/sectors/${encodeURIComponent(input.scheme)}/${encodeURIComponent(input.code)}/eod-snapshot${suffix}`,
      input.ifNoneMatch,
      internalSectorEodResourceSchema,
    ).then((response) =>
      response.status === 304
        ? response
        : { ...response, body: publicSectorEodResource(response.body) },
    );
  }

  /** 读取一个板块在固定观测 release 中的 verified 成分页，并删除内部身份 UUID。 */
  public listConstituents(input: {
    scheme: string;
    code: string;
    asOf?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
  }): Promise<UpstreamResponse<SectorConstituentPage>> {
    const parameters = new URLSearchParams({ limit: String(input.limit) });
    if (input.asOf !== undefined) parameters.set('asOf', input.asOf);
    if (input.cursor !== undefined) parameters.set('cursor', input.cursor);
    return this.request(
      `/internal/v1/sectors/${encodeURIComponent(input.scheme)}/${encodeURIComponent(input.code)}/constituents?${parameters.toString()}`,
      input.ifNoneMatch,
      internalSectorConstituentPageSchema,
    ).then((response) =>
      response.status === 304
        ? response
        : { ...response, body: publicSectorConstituentPage(response.body) },
    );
  }

  /** 读取一只证券在固定 release 中的板块观测归属，并删除服务内 UUID。 */
  public listEquitySectors(input: {
    exchange: string;
    symbol: string;
    scheme: string;
    asOf?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
  }): Promise<UpstreamResponse<EquitySectorPage>> {
    const parameters = new URLSearchParams({ scheme: input.scheme, limit: String(input.limit) });
    if (input.asOf !== undefined) parameters.set('asOf', input.asOf);
    if (input.cursor !== undefined) parameters.set('cursor', input.cursor);
    return this.request(
      `/internal/v1/equities/${encodeURIComponent(input.exchange)}/${encodeURIComponent(input.symbol)}/sectors?${parameters.toString()}`,
      input.ifNoneMatch,
      internalEquitySectorPageSchema,
    ).then((response) =>
      response.status === 304
        ? response
        : { ...response, body: publicEquitySectorPage(response.body) },
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

/** 去除 EOD 排行项中的内部 UUID，同时保留同一快照的排序和位置语义。 */
function publicSectorEodPage(input: InternalSectorEodPage): SectorEodPage {
  return {
    ...input,
    items: input.items.map((inputItem) => publicSectorEodItem(inputItem)),
  };
}

/** 去除单板块 EOD 报价内部 UUID，保证浏览器不依赖同步服务主键。 */
function publicSectorEodResource(input: InternalSectorEodResource): SectorEodResource {
  const resource = { ...input } as Record<string, unknown>;
  delete resource.sectorId;
  return resource as SectorEodResource;
}

/** 去除 EOD 排行项内部 UUID，同时保留同快照排序与稳定 position。 */
function publicSectorEodItem(
  input: InternalSectorEodPage['items'][number],
): SectorEodPage['items'][number] {
  const item = { ...input } as Record<string, unknown>;
  delete item.sectorId;
  return item as SectorEodPage['items'][number];
}

/** 删除内部板块和证券 UUID，构造公开板块到成分观测页。 */
function publicSectorConstituentPage(input: InternalSectorConstituentPage): SectorConstituentPage {
  return {
    sector: {
      scheme: input.sector.scheme,
      code: input.sector.code,
      name: input.sector.name,
    },
    release: input.release,
    snapshotObservedAt: input.snapshotObservedAt,
    carriedForward: input.carriedForward,
    items: input.items.map((item) => ({
      exchange: item.exchange,
      symbol: item.symbol,
      name: item.name,
      listingStatus: item.listingStatus,
      observedFrom: item.observedFrom,
      observedTo: item.observedTo,
    })),
    nextCursor: input.nextCursor,
  };
}

/** 删除内部板块和证券 UUID，构造公开证券到板块观测页。 */
function publicEquitySectorPage(input: InternalEquitySectorPage): EquitySectorPage {
  return {
    equity: {
      exchange: input.equity.exchange,
      symbol: input.equity.symbol,
      name: input.equity.name,
      listingStatus: input.equity.listingStatus,
    },
    scheme: input.scheme,
    release: input.release,
    items: input.items.map((item) => ({
      scheme: item.scheme,
      code: item.code,
      name: item.name,
      observedFrom: item.observedFrom,
      observedTo: item.observedTo,
      snapshotObservedAt: item.snapshotObservedAt,
      carriedForward: item.carriedForward,
    })),
    nextCursor: input.nextCursor,
  };
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
