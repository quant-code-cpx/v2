import { HttpStatus, Injectable } from '@nestjs/common';
import { z, type ZodType } from 'zod';

import { AppConfigService } from '../../config/app-config.service.js';
import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import {
  equityDetailSchema,
  equityPageSchema,
  internalEquityDetailSchema,
  internalEquityPageSchema,
  internalListingStatusHistoryPageSchema,
  listingStatusHistoryPageSchema,
  type EquityDetail,
  type EquityPage,
  type InternalEquityDetail,
  type InternalEquityPage,
  type InternalListingStatusHistoryPage,
  type ListingStatusHistoryPage,
} from '../contracts/equity-instrument.contract.js';

type FetchLike = typeof fetch;
type ConflictCode = 'snapshot-expired' | 'identity-resolution-conflict';

/** 约束每个公开端点可以暴露的冲突码及下游异常载荷的安全回退。 */
type ConflictPolicy = {
  allowed: readonly ConflictCode[];
  fallback: ConflictCode;
};

type NotModified = {
  status: 304;
  etag: string;
  dataVersion: string;
};

type VersionedBody = { dataVersion: string | null };

type UpstreamSuccess<T extends VersionedBody> = {
  status: 200;
  etag: string;
  dataVersion: string;
  body: T;
};

/** 描述保留条件请求状态的下游读取结果。 */
export type ConditionalRead<T extends VersionedBody> = NotModified | UpstreamSuccess<T>;

const downstreamProblemCodeSchema = z
  .object({
    code: z.enum(['snapshot-expired', 'identity-resolution-conflict', 'publication-unavailable']),
  })
  .passthrough();

/** 通过内部 0009 契约读取证券主数据，并裁剪所有服务内身份。 */
@Injectable()
export class EquityInstrumentClient {
  /** 使用受校验配置和可替换 fetch 构造防腐边界。 */
  public constructor(
    private readonly config: AppConfigService,
    private readonly fetcher: FetchLike = fetch,
  ) {}

  /** 读取一页证券目录，并将内部条目投影为 0010 公开合同。 */
  public listEquities(input: {
    exchange?: string | undefined;
    statuses?: readonly string[] | undefined;
    query?: string | undefined;
    asOf?: string | undefined;
    knownAt?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<ConditionalRead<EquityPage>> {
    const parameters = new URLSearchParams({ limit: String(input.limit) });
    setOptional(parameters, 'exchange', input.exchange);
    for (const status of input.statuses ?? []) parameters.append('status', status);
    setOptional(parameters, 'query', input.query);
    setOptional(parameters, 'asOf', input.asOf);
    setOptional(parameters, 'knownAt', input.knownAt);
    setOptional(parameters, 'cursor', input.cursor);
    return this.request(
      `/internal/v1/equities?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      internalEquityPageSchema,
      { allowed: ['snapshot-expired'], fallback: 'snapshot-expired' },
    ).then((result) =>
      result.status === 304 ? result : { ...result, body: publicEquityPage(result.body) },
    );
  }

  /** 按交易所、代码和双时态切片读取单一证券。 */
  public getEquity(input: {
    exchange: string;
    symbol: string;
    asOf?: string | undefined;
    knownAt?: string | undefined;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<ConditionalRead<EquityDetail>> {
    const parameters = new URLSearchParams();
    setOptional(parameters, 'asOf', input.asOf);
    setOptional(parameters, 'knownAt', input.knownAt);
    const suffix = querySuffix(parameters);
    return this.request(
      `/internal/v1/equities/${encodeURIComponent(input.exchange)}/${encodeURIComponent(input.symbol)}${suffix}`,
      input.ifNoneMatch,
      input.requestId,
      internalEquityDetailSchema,
      {
        allowed: ['identity-resolution-conflict'],
        fallback: 'identity-resolution-conflict',
      },
    ).then((result) =>
      result.status === 304 ? result : { ...result, body: publicEquityDetail(result.body) },
    );
  }

  /** 读取唯一证券的上市生命周期历史，并裁剪内部证券 UUID。 */
  public listListingStatusHistory(input: {
    exchange: string;
    symbol: string;
    asOf?: string | undefined;
    effectiveFrom?: string | undefined;
    effectiveTo?: string | undefined;
    knownAt?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<ConditionalRead<ListingStatusHistoryPage>> {
    const parameters = new URLSearchParams({ limit: String(input.limit) });
    setOptional(parameters, 'asOf', input.asOf);
    setOptional(parameters, 'effectiveFrom', input.effectiveFrom);
    setOptional(parameters, 'effectiveTo', input.effectiveTo);
    setOptional(parameters, 'knownAt', input.knownAt);
    setOptional(parameters, 'cursor', input.cursor);
    return this.request(
      `/internal/v1/equities/${encodeURIComponent(input.exchange)}/${encodeURIComponent(input.symbol)}/listing-status-history?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      internalListingStatusHistoryPageSchema,
      {
        allowed: ['snapshot-expired', 'identity-resolution-conflict'],
        fallback: input.cursor === undefined ? 'identity-resolution-conflict' : 'snapshot-expired',
      },
    ).then((result) =>
      result.status === 304
        ? result
        : { ...result, body: publicListingStatusHistoryPage(result.body) },
    );
  }

  /** 发起带内部凭据、超时、请求关联和严格响应校验的 GET。 */
  private async request<T extends VersionedBody>(
    path: string,
    ifNoneMatch: string | undefined,
    requestId: string,
    schema: ZodType<T>,
    conflictPolicy: ConflictPolicy,
  ): Promise<ConditionalRead<T>> {
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
    if (response.status === 304) {
      if (!validEtag(etag) || !validDataVersion(dataVersion)) throw dependencyUnavailable();
      return { status: 304, etag, dataVersion };
    }
    if (!response.ok) throw await upstreamProblem(response, conflictPolicy);

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

/** 仅在值存在时添加查询参数，避免把 `undefined` 变成字符串。 */
function setOptional(parameters: URLSearchParams, key: string, value: string | undefined): void {
  if (value !== undefined) parameters.set(key, value);
}

/** 只在存在参数时添加问号。 */
function querySuffix(parameters: URLSearchParams): string {
  const query = parameters.toString();
  return query.length === 0 ? '' : `?${query}`;
}

/** 校验下游只返回受控强 ETag，禁止弱校验器或换行进入公开响应头。 */
function validEtag(value: string | null): value is string {
  return value !== null && /^"[^"\r\n]{1,252}"$/.test(value);
}

/** 校验下游 publication 版本是规范 UUID。 */
function validDataVersion(value: string | null): value is string {
  return value !== null && z.string().uuid().safeParse(value).success;
}

/** 移除目录中每条证券的内部 UUID，并用公开 schema 再校验页上限。 */
function publicEquityPage(input: InternalEquityPage): EquityPage {
  return parsePublicResponse(equityPageSchema, {
    ...input,
    items: input.items.map(publicEquityItem),
  });
}

/** 移除单证券详情的内部 UUID。 */
function publicEquityDetail(input: InternalEquityDetail): EquityDetail {
  /** 保留 resolved publication 的组件血缘，公开层只裁剪内部证券 UUID。 */
  return parsePublicResponse(equityDetailSchema, {
    identifier: input.identifier,
    name: input.name,
    listing: input.listing,
    dataVersion: input.dataVersion,
    publishedAt: input.publishedAt,
    effectiveAsOf: input.effectiveAsOf,
    requestedKnownAt: input.requestedKnownAt,
    publicationScope: input.publicationScope,
    componentPublications: input.componentPublications,
  });
}

/** 移除上市状态历史页的内部 UUID。 */
function publicListingStatusHistoryPage(
  input: InternalListingStatusHistoryPage,
): ListingStatusHistoryPage {
  /** 保留历史项来源和 resolved 组件血缘，公开层只裁剪内部证券 UUID。 */
  return parsePublicResponse(listingStatusHistoryPageSchema, {
    exchange: input.exchange,
    symbol: input.symbol,
    items: input.items,
    nextCursor: input.nextCursor,
    dataVersion: input.dataVersion,
    publishedAt: input.publishedAt,
    requestedKnownAt: input.requestedKnownAt,
    publicationScope: input.publicationScope,
    componentPublications: input.componentPublications,
  });
}

/** 只选择公开证券条目允许的字段。 */
function publicEquityItem(input: InternalEquityPage['items'][number]): EquityPage['items'][number] {
  return {
    identifier: input.identifier,
    name: input.name,
    listing: input.listing,
  };
}

/** 用公开契约复核投影结果，并把契约漂移隐藏为稳定的依赖不可用错误。 */
function parsePublicResponse<T>(schema: ZodType<T>, input: unknown): T {
  const parsed = schema.safeParse(input);
  if (!parsed.success) throw dependencyUnavailable();
  return parsed.data;
}

/** 将下游不可用、内部鉴权异常或合同漂移统一成公开 503。 */
function dependencyUnavailable(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.SERVICE_UNAVAILABLE,
    'dependency-unavailable',
    'Equity instrument data is temporarily unavailable',
  );
}

/** 将允许公开的下游状态映射为稳定问题码，不转发下游详情。 */
async function upstreamProblem(
  response: Response,
  conflictPolicy: ConflictPolicy,
): Promise<PublicProblemException> {
  if (response.status === 400) {
    return new PublicProblemException(
      HttpStatus.BAD_REQUEST,
      'validation-error',
      'Equity instrument query is invalid',
    );
  }
  if (response.status === 404) {
    return new PublicProblemException(
      HttpStatus.NOT_FOUND,
      'not-found',
      'Equity instrument is not found',
    );
  }
  if (response.status === 409) {
    const downstreamCode = await readProblemCode(response);
    const code =
      downstreamCode !== undefined &&
      downstreamCode !== 'publication-unavailable' &&
      conflictPolicy.allowed.includes(downstreamCode)
        ? downstreamCode
        : conflictPolicy.fallback;
    return conflictProblem(code);
  }
  if (response.status === 429) {
    const retry = Number(response.headers.get('retry-after'));
    return new PublicProblemException(
      HttpStatus.TOO_MANY_REQUESTS,
      'rate-limited',
      'Equity instrument data is rate limited',
      Number.isSafeInteger(retry) && retry > 0 ? retry : undefined,
    );
  }
  if (response.status === 503 && (await readProblemCode(response)) === 'publication-unavailable') {
    return new PublicProblemException(
      HttpStatus.SERVICE_UNAVAILABLE,
      'publication-unavailable',
      'Equity instrument publication is unavailable',
    );
  }
  return dependencyUnavailable();
}

/** 仅从严格白名单中读取问题码，忽略下游其他问题字段。 */
async function readProblemCode(
  response: Response,
): Promise<ConflictCode | 'publication-unavailable' | undefined> {
  try {
    const parsed = downstreamProblemCodeSchema.safeParse(await response.json());
    return parsed.success ? parsed.data.code : undefined;
  } catch {
    return undefined;
  }
}

/** 把两种可公开冲突映射为不含内部标识的稳定错误。 */
function conflictProblem(code: ConflictCode): PublicProblemException {
  if (code === 'snapshot-expired') {
    return new PublicProblemException(
      HttpStatus.CONFLICT,
      code,
      'Published equity snapshot changed',
    );
  }
  return new PublicProblemException(HttpStatus.CONFLICT, code, 'Instrument identity is ambiguous');
}
