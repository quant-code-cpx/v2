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
  etag: string;
  dataVersion: string;
};

type UpstreamSuccess<T> = {
  status: 200;
  etag: string;
  dataVersion: string;
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
    requestId: string;
  }): Promise<UpstreamResponse<SectorPage>> {
    const parameters = new URLSearchParams({ scheme: input.scheme, limit: String(input.limit) });
    if (input.query !== undefined) parameters.set('query', input.query);
    if (input.cursor !== undefined) parameters.set('cursor', input.cursor);
    return this.request(
      `/internal/v1/sectors?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      internalSectorPageSchema,
    ).then((response) =>
      response.status === 304 ? response : { ...response, body: publicSectorPage(response.body) },
    );
  }

  /** 读取同步期已物化的正式周期 K 线页，API 请求线程不执行跨日聚合。 */
  public listBars(input: {
    scheme: string;
    code: string;
    period: string;
    start: string;
    end: string;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
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
      input.requestId,
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
    requestId: string;
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
      input.requestId,
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
    requestId: string;
  }): Promise<UpstreamResponse<SectorEodResource>> {
    const parameters = new URLSearchParams();
    if (input.asOf !== undefined) parameters.set('asOf', input.asOf);
    const suffix = input.asOf === undefined ? '' : `?${parameters.toString()}`;
    return this.request(
      `/internal/v1/sectors/${encodeURIComponent(input.scheme)}/${encodeURIComponent(input.code)}/eod-snapshot${suffix}`,
      input.ifNoneMatch,
      input.requestId,
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
    requestId: string;
  }): Promise<UpstreamResponse<SectorConstituentPage>> {
    const parameters = new URLSearchParams({ limit: String(input.limit) });
    const normalizedAsOf = normalizeMembershipAsOf(input.asOf);
    if (normalizedAsOf !== undefined) parameters.set('asOf', normalizedAsOf);
    if (input.cursor !== undefined) parameters.set('cursor', input.cursor);
    return this.request(
      `/internal/v1/sectors/${encodeURIComponent(input.scheme)}/${encodeURIComponent(input.code)}/constituents?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      internalSectorConstituentPageSchema,
    ).then((response) =>
      response.status === 304
        ? response
        : { ...response, body: publicSectorConstituentPage(response.body) },
    );
  }

  /** 读取一只证券在固定 release 中的板块观测归属，并删除服务内 UUID。 */
  public async listEquitySectors(input: {
    exchange: string;
    symbol: string;
    scheme: string;
    dataVersion: string;
    identityAsOf: string;
    knownAt?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<UpstreamResponse<EquitySectorPage>> {
    const parameters = new URLSearchParams({
      scheme: input.scheme,
      dataVersion: input.dataVersion,
      identityAsOf: input.identityAsOf,
      limit: String(input.limit),
    });
    if (input.knownAt !== undefined) parameters.set('knownAt', input.knownAt);
    if (input.cursor !== undefined) parameters.set('cursor', input.cursor);
    const response = await this.request(
      `/internal/v1/equities/${encodeURIComponent(input.exchange)}/${encodeURIComponent(input.symbol)}/sectors?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      internalEquitySectorPageSchema,
      input.dataVersion,
    );
    if (response.status === 304) return response;
    if (
      response.body.dataVersion !== input.dataVersion ||
      response.body.identityAsOf !== input.identityAsOf ||
      response.body.scheme !== input.scheme ||
      response.body.equity.exchange !== input.exchange ||
      response.body.equity.symbol !== input.symbol
    ) {
      throw dependencyUnavailable();
    }
    return { ...response, body: publicEquitySectorPage(response.body) };
  }

  /** 发起有认证、超时和严格合同校验的只读下游请求。 */
  private async request<T>(
    path: string,
    ifNoneMatch: string | undefined,
    requestId: string,
    schema: ZodType<T>,
    expectedDataVersion?: string,
  ): Promise<UpstreamResponse<T>> {
    if (!validRequestId(requestId)) throw dependencyUnavailable();
    const url = new URL(path, this.config.dataSyncInternalBaseUrl);
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
    if (response.headers.get('x-request-id') !== requestId) {
      await response.body?.cancel();
      throw dependencyUnavailable();
    }
    // 调用方指定 publication 时，304 与 200 都必须回到同一版本，禁止缓存命中掩盖快照漂移。
    if (response.status === 304) {
      if (
        !validEtag(etag) ||
        !validDataVersion(dataVersion) ||
        (expectedDataVersion !== undefined && dataVersion !== expectedDataVersion)
      ) {
        throw dependencyUnavailable();
      }
      return { status: 304, etag, dataVersion };
    }
    if (!response.ok) throw upstreamProblem(response.status, response.headers.get('retry-after'));
    try {
      const body = schema.parse(await response.json());
      const bodyDataVersion = sectorBodyDataVersion(body);
      if (
        !validEtag(etag) ||
        !validDataVersion(dataVersion) ||
        bodyDataVersion !== dataVersion ||
        (expectedDataVersion !== undefined && dataVersion !== expectedDataVersion)
      ) {
        throw dependencyUnavailable();
      }
      return { status: 200, etag, dataVersion, body };
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
    identityAsOf: input.identityAsOf,
    dataVersion: input.dataVersion,
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

/** 从板块顶层 publication 或 membership release 中读取稳定数据版本。 */
function sectorBodyDataVersion(value: unknown): string | undefined {
  if (typeof value !== 'object' || value === null) return undefined;
  if ('dataVersion' in value && typeof value.dataVersion === 'string') {
    return value.dataVersion;
  }
  if (
    'release' in value &&
    typeof value.release === 'object' &&
    value.release !== null &&
    'dataVersion' in value.release &&
    typeof value.release.dataVersion === 'string'
  ) {
    return value.release.dataVersion;
  }
  return undefined;
}

/** 把页面 date-only 快照规范为上海日末，完整 RFC3339 时间则保持原样。 */
function normalizeMembershipAsOf(value: string | undefined): string | undefined {
  if (value === undefined) return undefined;
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T23:59:59+08:00` : value;
}

/** 校验内部板块读端返回不含换行的强 ETag。 */
function validEtag(value: string | null): value is string {
  return value !== null && /^"[^"\r\n]{1,252}"$/.test(value);
}

/** 校验内部板块读端 publication 使用 UUID。 */
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
