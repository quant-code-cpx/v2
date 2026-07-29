import { HttpStatus, Injectable } from '@nestjs/common';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { AppConfigService } from '../../config/app-config.service.js';
import {
  marketDataQueryRequestSchema,
  marketDataQueryResponseSchema,
  type MarketDataQueryRequest,
  type MarketDataQueryResponse,
} from '../contracts/market-data-access.contract.js';

/** 表示可在单元测试中替换的标准 Fetch 实现。 */
type FetchLike = typeof fetch;

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
    const request = marketDataQueryRequestSchema.parse(input.request);
    const response = await this.post('/internal/v1/market-data/query', request, input.requestId);
    const dataVersion = response.headers.get('x-data-version');
    try {
      const body = marketDataQueryResponseSchema.parse(await response.json());
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
