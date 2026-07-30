import { HttpStatus, Injectable } from '@nestjs/common';
import { createHash } from 'node:crypto';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { AppConfigService } from '../../config/app-config.service.js';
import {
  STOCK_CONNECT_RESPONSE_BYTES,
  StockConnectClient,
  type StockConnectClientRead,
} from '../../data-sync/clients/stock-connect.client.js';
import type {
  StockConnectActiveSecurityPage,
  StockConnectChannelResponse,
  StockConnectOverviewResponse,
  StockConnectReadinessResponse,
  StockConnectSecurityContextResponse,
} from '../../data-sync/contracts/stock-connect.contract.js';
import {
  stockConnectActiveSecurityQuerySchema,
  stockConnectChannelQuerySchema,
  stockConnectOverviewQuerySchema,
  stockConnectReadinessQuerySchema,
  stockConnectSecurityContextQuerySchema,
} from '../../data-sync/contracts/stock-connect.contract.js';
import type {
  StockConnectActiveSecurityQueryDto,
  StockConnectChannelQueryDto,
  StockConnectOverviewQueryDto,
  StockConnectReadinessQueryDto,
  StockConnectSecurityContextQueryDto,
} from './dto/stock-connect-query.dto.js';

/** 表示公开读取的正常响应或 If-None-Match 命中的空响应。 */
export type StockConnectConditionalRead<T> =
  | { status: 200; dataVersion: string; etag: string; body: T }
  | { status: 204; dataVersion: string; etag: string };

/** 编排认证用户的沪深港通总览、readiness、通道、活跃证券与证券上下文读取。 */
@Injectable()
export class StockConnectService {
  /** 注入专用 data-sync 防腐 Client 与功能发布开关。 */
  public constructor(
    private readonly client: StockConnectClient,
    private readonly config: AppConfigService,
  ) {}

  /** 查询所选通道最后一个共同完成的真实 publication。 */
  public async overview(
    query: StockConnectOverviewQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<StockConnectConditionalRead<StockConnectOverviewResponse>> {
    this.assertEnabled();
    assertIfNoneMatch(ifNoneMatch);
    const request = parseQuery(stockConnectOverviewQuerySchema, query);
    return this.finalize(
      this.client.overview(request, requestId),
      ifNoneMatch,
      STOCK_CONNECT_RESPONSE_BYTES.overview,
      'queryStockConnectOverview',
      { ...request, channels: [...request.channels].sort() },
    );
  }

  /** 查询候选交易日逐通道准备状态，不改变或包装业务 publication。 */
  public async readiness(
    query: StockConnectReadinessQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<StockConnectConditionalRead<StockConnectReadinessResponse>> {
    this.assertEnabled();
    assertIfNoneMatch(ifNoneMatch);
    const request = parseQuery(stockConnectReadinessQuerySchema, query);
    return this.finalize(
      this.client.readiness(request, requestId),
      ifNoneMatch,
      STOCK_CONNECT_RESPONSE_BYTES.readiness,
      'queryStockConnectReadiness',
      { ...request, channels: [...request.channels].sort() },
    );
  }

  /** 查询单条通道的真实日终统计、额度、状态和趋势。 */
  public async channel(
    query: StockConnectChannelQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<StockConnectConditionalRead<StockConnectChannelResponse>> {
    this.assertEnabled();
    assertIfNoneMatch(ifNoneMatch);
    const request = parseQuery(stockConnectChannelQuerySchema, query);
    return this.finalize(
      this.client.channel(request, requestId),
      ifNoneMatch,
      STOCK_CONNECT_RESPONSE_BYTES.channel,
      'queryStockConnectChannel',
      request,
    );
  }

  /** 查询官方来源活跃证券榜或仅在该榜内可用的净额排序。 */
  public async activeSecurities(
    query: StockConnectActiveSecurityQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<StockConnectConditionalRead<StockConnectActiveSecurityPage>> {
    this.assertEnabled();
    assertIfNoneMatch(ifNoneMatch);
    const request = parseQuery(stockConnectActiveSecurityQuerySchema, query);
    return this.finalize(
      this.client.activeSecurities(request, requestId),
      ifNoneMatch,
      STOCK_CONNECT_RESPONSE_BYTES.activeSecurities,
      'queryStockConnectActiveSecurities',
      request,
    );
  }

  /** 查询稳定证券引用在互联互通通道内的历史表现。 */
  public async securityContext(
    query: StockConnectSecurityContextQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<StockConnectConditionalRead<StockConnectSecurityContextResponse>> {
    this.assertEnabled();
    assertIfNoneMatch(ifNoneMatch);
    const request = parseQuery(stockConnectSecurityContextQuerySchema, query);
    return this.finalize(
      this.client.securityContext(request, requestId),
      ifNoneMatch,
      STOCK_CONNECT_RESPONSE_BYTES.securityContext,
      'queryStockConnectSecurityContext',
      request,
    );
  }

  /** 校验公开响应预算，并把数据版本命中转换为 POST 204。 */
  private async finalize<T>(
    pending: Promise<StockConnectClientRead<T>>,
    ifNoneMatch: string | undefined,
    maximumBytes: number,
    operationId: string,
    request: unknown,
  ): Promise<StockConnectConditionalRead<T>> {
    const result = await pending;
    const serializedBytes = new TextEncoder().encode(JSON.stringify(result.body)).byteLength;
    if (serializedBytes > maximumBytes) {
      throw new PublicProblemException(
        HttpStatus.SERVICE_UNAVAILABLE,
        'UPSTREAM_UNAVAILABLE',
        'Stock-connect response exceeds its safety budget',
      );
    }
    const etag = createStockConnectEtag(operationId, request, result.dataVersion);
    if (ifNoneMatch === etag) {
      return { status: 204, dataVersion: result.dataVersion, etag };
    }
    return { status: 200, dataVersion: result.dataVersion, etag, body: result.body };
  }

  /** 在三服务真实链路验收前保持公开路由失败关闭，不提供样本响应。 */
  private assertEnabled(): void {
    if (!this.config.stockConnectApiEnabled) {
      throw new PublicProblemException(
        HttpStatus.SERVICE_UNAVAILABLE,
        'UPSTREAM_UNAVAILABLE',
        'Stock-connect API is not enabled',
      );
    }
  }
}

/** 限制条件缓存头为合同允许的单个标准强 ETag。 */
function assertIfNoneMatch(ifNoneMatch: string | undefined): void {
  if (
    ifNoneMatch !== undefined &&
    (ifNoneMatch.length < 1 ||
      ifNoneMatch.length > 160 ||
      !/^"[A-Za-z0-9._:-]{1,158}"$/.test(ifNoneMatch))
  ) {
    throw new PublicProblemException(
      HttpStatus.BAD_REQUEST,
      'VALIDATION_FAILED',
      'If-None-Match is invalid',
    );
  }
}

/** 从 operation、规范请求表示和 dataVersion 生成稳定、无歧义的强 ETag。 */
export function createStockConnectEtag(
  operationId: string,
  request: unknown,
  dataVersion: string,
): string {
  const representation = `${operationId}\n${canonicalJson(request)}\n${dataVersion}`;
  return `"${createHash('sha256').update(representation).digest('base64url')}"`;
}

/** 按字典序递归排列对象键，保证同一逻辑请求不受输入字段顺序影响。 */
function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== 'object') {
    const encoded = JSON.stringify(value);
    if (encoded === undefined) throw new Error('request contains a non-JSON value');
    return encoded;
  }
  if (Array.isArray(value)) {
    const items: string[] = [];
    for (const item of value) items.push(canonicalJson(item));
    return `[${items.join(',')}]`;
  }
  const object = value as Record<string, unknown>;
  const keys = Object.keys(object).sort();
  const properties: string[] = [];
  for (const key of keys) {
    properties.push(`${JSON.stringify(key)}:${canonicalJson(object[key])}`);
  }
  return `{${properties.join(',')}}`;
}

/** 将公开 DTO 再次收敛为严格机器合同，并稳定映射校验失败。 */
function parseQuery<T>(
  schema: { safeParse(input: unknown): { success: true; data: T } | { success: false } },
  input: unknown,
): T {
  const result = schema.safeParse(input);
  if (result.success) return result.data;
  throw new PublicProblemException(
    HttpStatus.BAD_REQUEST,
    'VALIDATION_FAILED',
    'Stock-connect query is invalid',
  );
}
