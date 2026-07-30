import { HttpStatus, Injectable } from '@nestjs/common';
import { createHash } from 'node:crypto';
import type { ZodType } from 'zod';
import { z } from 'zod';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { AppConfigService } from '../../config/app-config.service.js';
import {
  EQUITY_DISCOVERY_CAPABILITY_COLUMNS,
  EQUITY_DISCOVERY_CAPABILITY_SORT_FIELDS,
  equityDataStatusResponseSchema,
  equityEventResponseSchema,
  equitySearchResponseSchema,
  internalEquityDataStatusRequestSchema,
  internalEquityEventRequestSchema,
  internalEquityEventResponseSchema,
  internalEquitySearchRequestSchema,
  internalEquitySearchResponseSchema,
  type EquityDataStatusResponse,
  type EquityEventResponse,
  type EquitySearchResponse,
  type InternalEquityDataStatusRequest,
  type InternalEquityEventRequest,
  type InternalEquityEventResponse,
  type InternalEquitySearchRequest,
  type InternalEquitySearchResponse,
} from '../contracts/equity-workspace.contract.js';

type FetchLike = typeof fetch;
type WorkspaceOperation = 'SEARCH' | 'EVENTS' | 'DATA_STATUS';

/** 描述下游条件读取命中，保留强 ETag 与数据版本。 */
type NotModified = {
  status: 304;
  etag: string;
  dataVersion: string;
};

/** 描述经过严格合同验证的下游成功响应。 */
type UpstreamSuccess<T> = {
  status: 200;
  etag: string | undefined;
  dataVersion: string | undefined;
  body: T;
};

/** 描述股票中心防腐层保留的条件读取状态。 */
export type EquityWorkspaceConditionalRead<T> = NotModified | UpstreamSuccess<T>;

const MAX_CONCURRENT_REQUESTS = 64;
const MAX_RESPONSE_BYTES = 4 * 1024 * 1024;

const downstreamProblemSchema = z
  .object({
    code: z.string().regex(/^[a-z][a-z0-9-]{0,79}$/),
  })
  .passthrough();

/** 在防腐层内部标记无 publication，不把下游问题正文暴露给 Controller。 */
class PublicationUnavailableSignal extends Error {}

/** 通过三条版本化内部 POST reader 访问真实股票中心数据。 */
@Injectable()
export class EquityWorkspaceClient {
  private activeRequests = 0;

  /** 使用集中配置和可替换 fetch 构造有界下游访问边界。 */
  public constructor(
    private readonly config: AppConfigService,
    private readonly fetcher: FetchLike = fetch,
  ) {}

  /** 在单一 discovery publication 上执行搜索、筛选、排序和 cursor 分页。 */
  public async search(input: {
    body: InternalEquitySearchRequest;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<EquityWorkspaceConditionalRead<EquitySearchResponse>> {
    const requestBody = internalEquitySearchRequestSchema.parse(input.body);
    try {
      const result = await this.request(
        '/internal/v1/equity-discovery/query',
        requestBody,
        input.ifNoneMatch,
        input.requestId,
        internalEquitySearchResponseSchema,
        'SEARCH',
      );
      if (result.status === 304) return result;
      return {
        ...result,
        body: projectSearchResponse(result.body),
      };
    } catch (error: unknown) {
      if (error instanceof PublicationUnavailableSignal) {
        return {
          status: 200,
          etag: undefined,
          dataVersion: undefined,
          body: unavailableSearch(requestBody.limit),
        };
      }
      throw error;
    }
  }

  /** 通过公开证券身份读取公司行动、业绩和交易事件。 */
  public async searchEvents(input: {
    exchange: string;
    symbol: string;
    body: InternalEquityEventRequest;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<EquityWorkspaceConditionalRead<EquityEventResponse>> {
    const requestBody = internalEquityEventRequestSchema.parse(input.body);
    const path =
      `/internal/v1/equities/${encodeURIComponent(input.exchange)}/` +
      `${encodeURIComponent(input.symbol)}/events/query`;
    try {
      const result = await this.request(
        path,
        requestBody,
        input.ifNoneMatch,
        input.requestId,
        internalEquityEventResponseSchema,
        'EVENTS',
      );
      if (result.status === 304) return result;
      return {
        ...result,
        body: projectEventResponse(result.body),
      };
    } catch (error: unknown) {
      if (error instanceof PublicationUnavailableSignal) {
        return {
          status: 200,
          etag: undefined,
          dataVersion: undefined,
          body: unavailableEvents(requestBody.limit),
        };
      }
      throw error;
    }
  }

  /** 读取一只证券多个详情数据集的独立 availability 与 freshness。 */
  public async getDataStatus(input: {
    exchange: string;
    symbol: string;
    body: InternalEquityDataStatusRequest;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<EquityWorkspaceConditionalRead<EquityDataStatusResponse>> {
    const requestBody = internalEquityDataStatusRequestSchema.parse(input.body);
    const path =
      `/internal/v1/equities/${encodeURIComponent(input.exchange)}/` +
      `${encodeURIComponent(input.symbol)}/data-status/query`;
    const result = await this.request(
      path,
      requestBody,
      input.ifNoneMatch,
      input.requestId,
      equityDataStatusResponseSchema,
      'DATA_STATUS',
    );
    if (result.status === 304) return result;
    return {
      ...result,
      body: equityDataStatusResponseSchema.parse(result.body),
    };
  }

  /** 发起内部 POST，执行并发隔离、单次连接重试、响应头与严格 Zod 校验。 */
  private async request<T>(
    requestPath: string,
    body: unknown,
    ifNoneMatch: string | undefined,
    requestId: string,
    schema: ZodType<T>,
    operation: WorkspaceOperation,
  ): Promise<EquityWorkspaceConditionalRead<T>> {
    if (this.activeRequests >= MAX_CONCURRENT_REQUESTS) throw dependencyUnavailable();
    this.activeRequests += 1;
    try {
      const url = new URL(requestPath, this.config.dataSyncInternalBaseUrl);
      const headers: Record<string, string> = {
        Accept: 'application/json',
        Authorization: `Bearer ${this.config.dataSyncInternalBearerToken}`,
        'Content-Type': 'application/json',
        'X-Request-Id': requestId,
      };
      if (ifNoneMatch !== undefined) headers['If-None-Match'] = ifNoneMatch;
      const response = await this.fetchWithSingleConnectionRetry(url, headers, body);
      const etag = response.headers.get('etag') ?? undefined;
      const dataVersion = response.headers.get('x-data-version') ?? undefined;
      const responseRequestId = response.headers.get('x-request-id');
      if (responseRequestId !== requestId) throw dependencyUnavailable();
      if (response.status === 304) {
        if (!isStrongEtag(etag) || !isDataVersion(dataVersion)) throw dependencyUnavailable();
        return { status: 304, etag, dataVersion };
      }
      if (!response.ok) throw await upstreamProblem(response, operation);
      if (!isStrongEtag(etag)) throw dependencyUnavailable();
      const internalBody = await parseJsonResponse(response, schema);
      const bodyVersion = releaseDataVersion(internalBody);
      if (
        (dataVersion !== undefined && !isDataVersion(dataVersion)) ||
        (bodyVersion !== undefined && dataVersion !== bodyVersion) ||
        (operation === 'DATA_STATUS' && !isDataVersion(dataVersion))
      ) {
        throw dependencyUnavailable();
      }
      return { status: 200, etag, dataVersion, body: internalBody };
    } finally {
      this.activeRequests -= 1;
    }
  }

  /** 只对未收到 HTTP 响应的连接错误重试一次；超时与任何 HTTP 状态不重试。 */
  private async fetchWithSingleConnectionRetry(
    url: URL,
    headers: Record<string, string>,
    body: unknown,
  ): Promise<Response> {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        return await this.fetcher(url, {
          method: 'POST',
          headers,
          body: JSON.stringify(body),
          signal: AbortSignal.timeout(this.config.dataSyncInternalRequestTimeoutMs),
        });
      } catch (error: unknown) {
        if (attempt === 0 && !isTimeoutError(error)) continue;
        throw dependencyUnavailable();
      }
    }
    throw dependencyUnavailable();
  }
}

/** 把内部生命周期字段改为公开命名，并裁剪 release dataset。 */
function projectSearchResponse(input: InternalEquitySearchResponse): EquitySearchResponse {
  return equitySearchResponseSchema.parse({
    ...input,
    release: input.release === null ? null : projectSearchRelease(input.release),
    records: input.records.map(projectDiscoveryRecord),
  });
}

/** 把一条内部 discovery 记录投影为不含服务内身份的公开记录。 */
function projectDiscoveryRecord(
  input: InternalEquitySearchResponse['records'][number],
): EquitySearchResponse['records'][number] {
  return {
    ...input,
    statuses: {
      listingStatus: input.statuses.lifecycleStatus,
      tradingStatus: input.statuses.tradingStatus,
      tradingStatusReason: input.statuses.tradingStatusReason,
      listedOn: input.statuses.listedOn,
      delistedOn: input.statuses.delistedOn,
    },
  };
}

/** 把内部事件主键哈希成公开稳定引用，并裁剪 release dataset。 */
function projectEventResponse(input: InternalEquityEventResponse): EquityEventResponse {
  return equityEventResponseSchema.parse({
    ...input,
    release: input.release === null ? null : projectEventRelease(input.release),
    events: input.events.map(projectEvent),
  });
}

/** 为事件生成不可逆公开引用，不暴露同步服务主键。 */
function projectEvent(
  input: InternalEquityEventResponse['events'][number],
): EquityEventResponse['events'][number] {
  const { eventId, ...publicFields } = input;
  const digest = createHash('sha256').update(eventId).digest('base64url');
  return { ...publicFields, eventRef: `evt_${digest}` };
}

/** 裁剪 discovery 服务间 dataset，同时保留 completeness。 */
function projectSearchRelease(
  input: NonNullable<InternalEquitySearchResponse['release']>,
): NonNullable<EquitySearchResponse['release']> {
  return {
    dataVersion: input.dataVersion,
    publishedAt: input.publishedAt,
    effectiveAsOf: input.effectiveAsOf,
    knowledgeCutoff: input.knowledgeCutoff,
    qualityStatus: input.qualityStatus,
    completeness: input.completeness,
  };
}

/** 裁剪事件聚合服务间 dataset，不为事件伪造 discovery 完整度。 */
function projectEventRelease(
  input: NonNullable<InternalEquityEventResponse['release']>,
): NonNullable<EquityEventResponse['release']> {
  return {
    dataVersion: input.dataVersion,
    publishedAt: input.publishedAt,
    effectiveAsOf: input.effectiveAsOf,
    knowledgeCutoff: input.knowledgeCutoff,
    qualityStatus: input.qualityStatus,
  };
}

/** 构造无 publication 的公开搜索状态；静态能力不是市场事实。 */
function unavailableSearch(limit: number): EquitySearchResponse {
  return equitySearchResponseSchema.parse({
    availability: 'UNAVAILABLE',
    reasonCode: 'NO_PUBLICATION',
    release: null,
    components: [],
    capabilities: {
      sortFields: [...EQUITY_DISCOVERY_CAPABILITY_SORT_FIELDS],
      columns: [...EQUITY_DISCOVERY_CAPABILITY_COLUMNS],
      maxLimit: 100,
    },
    records: [],
    page: { nextCursor: null, limit },
  });
}

/** 构造无 publication 的公开事件状态，不伪造合法空事件集。 */
function unavailableEvents(limit: number): EquityEventResponse {
  return equityEventResponseSchema.parse({
    availability: 'UNAVAILABLE',
    reasonCode: 'NO_PUBLICATION',
    release: null,
    events: [],
    page: { nextCursor: null, limit },
  });
}

/** 从带 release 的响应读取数据版本；状态聚合响应由响应头单独承载。 */
function releaseDataVersion(value: unknown): string | undefined {
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

/** 校验同步服务返回强 ETag，拒绝弱验证器和未加引号值。 */
function isStrongEtag(value: string | undefined): value is string {
  return value !== undefined && /^"[A-Za-z0-9._:-]{1,200}"$/.test(value);
}

/** 校验响应头数据版本为 UUID。 */
function isDataVersion(value: string | undefined): value is string {
  return value !== undefined && z.string().uuid().safeParse(value).success;
}

/** 判断 fetch 异常是否为硬超时，避免重试把交互 deadline 翻倍。 */
function isTimeoutError(error: unknown): boolean {
  return error instanceof Error && (error.name === 'AbortError' || error.name === 'TimeoutError');
}

/** 在有界体积内解析 JSON 并执行严格响应 schema。 */
async function parseJsonResponse<T>(response: Response, schema: ZodType<T>): Promise<T> {
  const contentType = response.headers.get('content-type')?.toLowerCase() ?? '';
  const declaredLength = Number(response.headers.get('content-length'));
  if (
    !contentType.includes('application/json') ||
    (Number.isFinite(declaredLength) && declaredLength > MAX_RESPONSE_BYTES)
  ) {
    throw dependencyUnavailable();
  }
  const payload = await response.text();
  if (Buffer.byteLength(payload, 'utf8') > MAX_RESPONSE_BYTES) throw dependencyUnavailable();
  try {
    return schema.parse(JSON.parse(payload) as unknown);
  } catch {
    throw dependencyUnavailable();
  }
}

/** 读取下游公开允许的问题码，忽略详情、内部标识和异常堆栈。 */
async function readProblemCode(response: Response): Promise<string | undefined> {
  try {
    const payload = await response.text();
    if (Buffer.byteLength(payload, 'utf8') > 16 * 1024) return undefined;
    const parsed = downstreamProblemSchema.safeParse(JSON.parse(payload) as unknown);
    return parsed.success ? parsed.data.code : undefined;
  } catch {
    return undefined;
  }
}

/** 将内部错误稳定映射为公开 400、404、409、429 或 503。 */
async function upstreamProblem(response: Response, operation: WorkspaceOperation): Promise<Error> {
  if (response.status === 400) {
    return new PublicProblemException(
      HttpStatus.BAD_REQUEST,
      'validation-error',
      'Equity workspace query is invalid',
    );
  }
  if (response.status === 404 && operation !== 'SEARCH') {
    return new PublicProblemException(
      HttpStatus.NOT_FOUND,
      'equity-not-found',
      'Equity is not found',
    );
  }
  if (response.status === 409) {
    const code = await readProblemCode(response);
    if (code === 'identity-resolution-conflict' || code === 'identity-incomplete') {
      return new PublicProblemException(
        HttpStatus.CONFLICT,
        'identity-resolution-conflict',
        'Equity identity is ambiguous for the requested date',
      );
    }
    if (code !== 'snapshot-expired') return dependencyUnavailable();
    return new PublicProblemException(
      HttpStatus.CONFLICT,
      'snapshot-expired',
      'Published equity snapshot changed',
    );
  }
  if (response.status === 429) {
    const retryAfter = Number(response.headers.get('retry-after'));
    return new PublicProblemException(
      HttpStatus.TOO_MANY_REQUESTS,
      'rate-limited',
      'Equity workspace data is rate limited',
      Number.isSafeInteger(retryAfter) && retryAfter > 0 ? retryAfter : undefined,
    );
  }
  if (response.status === 503) {
    const code = await readProblemCode(response);
    if (code === 'publication-unavailable' && operation !== 'DATA_STATUS') {
      return new PublicationUnavailableSignal();
    }
    if (code === 'publication-unavailable') {
      return new PublicProblemException(
        HttpStatus.SERVICE_UNAVAILABLE,
        'publication-unavailable',
        'Equity publication is unavailable',
      );
    }
  }
  return dependencyUnavailable();
}

/** 将网络、内部认证、合同漂移和非白名单状态统一为公开依赖不可用。 */
function dependencyUnavailable(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.SERVICE_UNAVAILABLE,
    'dependency-unavailable',
    'Equity workspace data is temporarily unavailable',
  );
}
