import { HttpStatus, Injectable } from '@nestjs/common';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { AppConfigService } from '../../config/app-config.service.js';
import {
  marketDataQueryRequestSchema,
  parseMarketDataQueryResponse,
  type MarketDataQueryRequest,
  type MarketDataQueryResponse,
} from '../contracts/market-data-access.contract.js';

/** 表示可在单元测试中替换的标准 Fetch 实现。 */
type FetchLike = typeof fetch;

/** 限制单页 typed market-data 响应，防止下游通过缺失或伪造长度头绕过内存边界。 */
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;

/** 通过内部 POST 合同读取 P0/P1 市场数据，绝不直连同步库或 Provider。 */
@Injectable()
export class MarketDataAccessClient {
  /** 使用集中配置与可替换 Fetch 构造跨服务防腐边界。 */
  public constructor(
    private readonly config: AppConfigService,
    private readonly fetcher: FetchLike = fetch,
  ) {}

  /** 读取一个 dataset 的单页 typed record，保留空结果而不是转成依赖故障。 */
  public async query(input: {
    request: unknown;
    requestId: string;
  }): Promise<MarketDataQueryResponse> {
    const parsedRequest = marketDataQueryRequestSchema.safeParse(input.request);
    if (!parsedRequest.success) throw invalidQuery();
    const request = parsedRequest.data;
    const response = await this.post('/internal/v1/market-data/query', request, input.requestId);
    const dataVersion = response.headers.get('x-data-version');
    const responseRequestId = response.headers.get('x-request-id');
    try {
      const body = parseMarketDataQueryResponse(await readBoundedJson(response), request);
      if (body.meta.requestId !== input.requestId || responseRequestId !== input.requestId) {
        throw dependencyUnavailable();
      }
      if (body.meta.availability === 'AVAILABLE') {
        if (
          !('dataVersion' in body.meta.release) ||
          dataVersion !== body.meta.release.dataVersion
        ) {
          throw dependencyUnavailable();
        }
      } else if (dataVersion !== null || body.records.length !== 0) {
        throw dependencyUnavailable();
      }
      return body;
    } catch (error) {
      if (error instanceof PublicProblemException) throw error;
      throw dependencyUnavailable();
    }
  }

  /** 发起带服务身份、关联标识和超时的只读内部 POST。 */
  private async post(
    requestPath: string,
    request: MarketDataQueryRequest,
    requestId: string,
  ): Promise<Response> {
    const url = new URL(requestPath, this.config.dataSyncInternalBaseUrl);
    let response: Response;
    try {
      response = await this.fetcher(url, {
        method: 'POST',
        headers: {
          Accept: 'application/json',
          Authorization: `Bearer ${this.config.dataSyncInternalBearerToken}`,
          'Content-Type': 'application/json',
          'X-Request-Id': requestId,
        },
        body: JSON.stringify(request),
        signal: AbortSignal.timeout(this.config.dataSyncInternalRequestTimeoutMs),
      });
    } catch {
      throw dependencyUnavailable();
    }
    if (!response.ok) throw upstreamProblem(response.status, response.headers.get('retry-after'));
    return response;
  }
}

/** 以流式字节计数解析 JSON，同时校验媒体类型、声明长度和真实响应大小。 */
async function readBoundedJson(response: Response): Promise<unknown> {
  const contentType = response.headers.get('content-type')?.split(';', 1)[0]?.trim().toLowerCase();
  if (contentType !== 'application/json') {
    throw dependencyUnavailable();
  }
  const contentLength = response.headers.get('content-length');
  if (contentLength !== null) {
    if (!/^\d+$/u.test(contentLength)) {
      throw dependencyUnavailable();
    }
    const declaredBytes = Number.parseInt(contentLength, 10);
    if (!Number.isSafeInteger(declaredBytes) || declaredBytes > MAX_RESPONSE_BYTES) {
      throw dependencyUnavailable();
    }
  }
  const body = response.body;
  if (body === null) {
    throw dependencyUnavailable();
  }
  const reader: ReadableStreamDefaultReader<Uint8Array> = body.getReader();
  const chunks: Uint8Array[] = [];
  let totalBytes = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      if (chunk.value === undefined) continue;
      totalBytes += chunk.value.byteLength;
      if (totalBytes > MAX_RESPONSE_BYTES) {
        try {
          await reader.cancel();
        } catch {
          // 超限已经决定拒绝响应；取消失败不能覆盖稳定的公开错误。
        }
        throw dependencyUnavailable();
      }
      chunks.push(chunk.value);
    }
  } catch (error) {
    if (error instanceof PublicProblemException) throw error;
    throw dependencyUnavailable();
  }
  const bytes = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    return JSON.parse(text) as unknown;
  } catch {
    throw dependencyUnavailable();
  }
}

/** 将公开查询结构或 ETF v2 白名单错误映射为稳定的 400。 */
function invalidQuery(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.BAD_REQUEST,
    'validation-error',
    'Market data query is invalid',
  );
}

/** 将网络、内部鉴权或响应合同漂移收敛为安全的公开 503。 */
function dependencyUnavailable(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.SERVICE_UNAVAILABLE,
    'dependency-unavailable',
    'Market data is temporarily unavailable',
  );
}

/** 仅映射稳定且面向调用方有意义的内部 POST 状态。 */
function upstreamProblem(status: number, retryAfter: string | null): PublicProblemException {
  if (status === 400 || status === 422) {
    return new PublicProblemException(
      HttpStatus.BAD_REQUEST,
      'validation-error',
      'Market data query is invalid',
    );
  }
  if (status === 404) {
    return new PublicProblemException(
      HttpStatus.NOT_FOUND,
      'not-found',
      'Market data is not found',
    );
  }
  if (status === 409) {
    return new PublicProblemException(
      HttpStatus.CONFLICT,
      'snapshot-expired',
      'Published market data snapshot changed',
    );
  }
  if (status === 429) {
    const seconds = Number(retryAfter);
    return new PublicProblemException(
      HttpStatus.TOO_MANY_REQUESTS,
      'rate-limited',
      'Market data is rate limited',
      Number.isSafeInteger(seconds) && seconds > 0 ? seconds : undefined,
    );
  }
  return dependencyUnavailable();
}
