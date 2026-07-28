import { BadRequestException, Injectable } from '@nestjs/common';

import type { EquityPathDto } from './dto/equity-path.dto.js';
import type { EquityTemporalQueryDto } from './dto/equity-temporal-query.dto.js';
import type { ListEquitiesQueryDto } from './dto/list-equities-query.dto.js';
import type { ListListingStatusHistoryQueryDto } from './dto/list-listing-status-history-query.dto.js';
import {
  EquityInstrumentClient,
  type ConditionalRead,
} from '../../data-sync/clients/equity-instrument.client.js';
import type {
  EquityDetail,
  EquityPage,
  ListingStatusHistoryPage,
} from '../../data-sync/contracts/equity-instrument.contract.js';

/** 编排认证用户的证券主数据读取与跨字段时间校验。 */
@Injectable()
export class EquityInstrumentService {
  /** 注入唯一允许访问同步服务的防腐 client。 */
  public constructor(private readonly client: EquityInstrumentClient) {}

  /** 返回一页已发布证券目录。 */
  public listEquities(
    query: ListEquitiesQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<ConditionalRead<EquityPage>> {
    assertKnownAt(query.knownAt);
    assertIfNoneMatch(ifNoneMatch);
    return this.client.listEquities({
      exchange: query.exchange,
      statuses: query.status,
      query: query.query,
      asOf: query.asOf,
      knownAt: query.knownAt,
      cursor: query.cursor,
      limit: query.limit,
      ifNoneMatch,
      requestId,
    });
  }

  /** 返回按双时态唯一解析的一只证券。 */
  public getEquity(
    path: EquityPathDto,
    query: EquityTemporalQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<ConditionalRead<EquityDetail>> {
    assertKnownAt(query.knownAt);
    assertIfNoneMatch(ifNoneMatch);
    return this.client.getEquity({
      exchange: path.exchange,
      symbol: path.symbol,
      asOf: query.asOf,
      knownAt: query.knownAt,
      ifNoneMatch,
      requestId,
    });
  }

  /** 返回一只证券的上市生命周期历史。 */
  public listListingStatusHistory(
    path: EquityPathDto,
    query: ListListingStatusHistoryQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<ConditionalRead<ListingStatusHistoryPage>> {
    assertKnownAt(query.knownAt);
    assertIfNoneMatch(ifNoneMatch);
    if (
      query.effectiveFrom !== undefined &&
      query.effectiveTo !== undefined &&
      query.effectiveFrom >= query.effectiveTo
    ) {
      throw new BadRequestException('effectiveFrom must be before effectiveTo');
    }
    return this.client.listListingStatusHistory({
      exchange: path.exchange,
      symbol: path.symbol,
      asOf: query.asOf,
      effectiveFrom: query.effectiveFrom,
      effectiveTo: query.effectiveTo,
      knownAt: query.knownAt,
      cursor: query.cursor,
      limit: query.limit,
      ifNoneMatch,
      requestId,
    });
  }
}

/** 拒绝未来知识时刻，避免产生平台尚未知道的读取视图。 */
function assertKnownAt(knownAt: string | undefined): void {
  if (knownAt !== undefined && Date.parse(knownAt) > Date.now()) {
    throw new BadRequestException('knownAt must not be in the future');
  }
}

/** 限制条件请求头长度，防止无界透传给内部服务。 */
function assertIfNoneMatch(ifNoneMatch: string | undefined): void {
  if (ifNoneMatch !== undefined && (ifNoneMatch.length < 1 || ifNoneMatch.length > 256)) {
    throw new BadRequestException('If-None-Match is invalid');
  }
}
