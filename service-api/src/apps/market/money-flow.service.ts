import { BadRequestException, Injectable } from '@nestjs/common';

import {
  MoneyFlowClient,
  type MoneyFlowConditionalRead,
} from '../../data-sync/clients/money-flow.client.js';
import type {
  MoneyFlowDailyPage,
  MoneyFlowMethodologyPage,
  MoneyFlowRankingPage,
} from '../../data-sync/contracts/money-flow.contract.js';
import type {
  EquityMoneyFlowPathDto,
  ListMoneyFlowDailyQueryDto,
  ListMoneyFlowMethodologiesQueryDto,
  ListMoneyFlowRankingQueryDto,
  MarketMoneyFlowPathDto,
  MoneyFlowMethodologyPathDto,
  SectorMoneyFlowPathDto,
} from './dto/money-flow.dto.js';

/** 编排认证用户的资金流方法学、来源日序列和供应商排行读取。 */
@Injectable()
export class MoneyFlowService {
  /** 注入唯一允许访问 0015 内部契约的防腐 client。 */
  public constructor(private readonly client: MoneyFlowClient) {}

  /** 读取公开可用的方法学目录。 */
  public listMethodologies(
    query: ListMoneyFlowMethodologiesQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<MoneyFlowConditionalRead<MoneyFlowMethodologyPage>> {
    assertIfNoneMatch(ifNoneMatch);
    return this.client.listMethodologies({
      semanticFamily: query.semanticFamily,
      methodologyStatus: query.methodologyStatus,
      scopeType: query.scopeType,
      cursor: query.cursor,
      limit: query.limit,
      ifNoneMatch,
      requestId,
    });
  }

  /** 读取一只证券的日期感知强身份日序列。 */
  public listEquityDaily(
    path: EquityMoneyFlowPathDto,
    query: ListMoneyFlowDailyQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<MoneyFlowConditionalRead<MoneyFlowDailyPage>> {
    return this.listDaily(
      path,
      query,
      `equities/${encodeURIComponent(path.exchange)}/${encodeURIComponent(path.symbol)}`,
      ifNoneMatch,
      requestId,
    );
  }

  /** 读取来源板块日序列，不聚合成分证券。 */
  public listSectorDaily(
    path: SectorMoneyFlowPathDto,
    query: ListMoneyFlowDailyQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<MoneyFlowConditionalRead<MoneyFlowDailyPage>> {
    return this.listDaily(
      path,
      query,
      `sectors/${encodeURIComponent(path.scheme)}/${encodeURIComponent(path.sectorCode)}`,
      ifNoneMatch,
      requestId,
    );
  }

  /** 读取来源市场 scope 日序列，不从证券或板块求和。 */
  public listMarketDaily(
    path: MarketMoneyFlowPathDto,
    query: ListMoneyFlowDailyQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<MoneyFlowConditionalRead<MoneyFlowDailyPage>> {
    return this.listDaily(
      path,
      query,
      `markets/${encodeURIComponent(path.marketCode)}`,
      ifNoneMatch,
      requestId,
    );
  }

  /** 读取 exact 或 latest 的不可变 supplier ranking。 */
  public listRanking(
    path: MoneyFlowMethodologyPathDto,
    query: ListMoneyFlowRankingQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<MoneyFlowConditionalRead<MoneyFlowRankingPage>> {
    assertIfNoneMatch(ifNoneMatch);
    const validWindow =
      (query.windowType === 'supplier_day' && query.windowSize === 1) ||
      (query.windowType === 'supplier_rolling' && query.windowSize > 1);
    if (!validWindow) {
      throw new BadRequestException('windowSize does not match windowType');
    }
    return this.client.listRanking({
      methodologyId: path.methodologyId,
      methodologyVersion: query.methodologyVersion,
      scopeType: query.scopeType,
      universe: query.universe,
      windowType: query.windowType,
      windowSize: query.windowSize,
      bucket: query.bucket,
      tradeDate: query.tradeDate,
      cursor: query.cursor,
      limit: query.limit,
      ifNoneMatch,
      requestId,
    });
  }

  /** 执行三类日序列共有的窗口、知识时点和条件请求校验。 */
  private listDaily(
    path: MoneyFlowMethodologyPathDto,
    query: ListMoneyFlowDailyQueryDto,
    scopePath: string,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<MoneyFlowConditionalRead<MoneyFlowDailyPage>> {
    if (query.start > query.end) {
      throw new BadRequestException('start must not be after end');
    }
    if (daysBetween(query.start, query.end) > 365) {
      throw new BadRequestException('money-flow date span exceeds 366 calendar days');
    }
    if (query.knownAt !== undefined && Date.parse(query.knownAt) > Date.now()) {
      throw new BadRequestException('knownAt must not be in the future');
    }
    assertIfNoneMatch(ifNoneMatch);
    return this.client.listDaily({
      methodologyId: path.methodologyId,
      methodologyVersion: query.methodologyVersion,
      scopePath,
      bucket: query.bucket,
      start: query.start,
      end: query.end,
      knownAt: query.knownAt,
      cursor: query.cursor,
      limit: query.limit,
      ifNoneMatch,
      requestId,
    });
  }
}

/** 计算两个严格 ISO 日期之间的 UTC 日数。 */
function daysBetween(start: string, end: string): number {
  return (Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86_400_000;
}

/** 限制条件请求头长度，避免无界透传。 */
function assertIfNoneMatch(ifNoneMatch: string | undefined): void {
  if (ifNoneMatch !== undefined && (ifNoneMatch.length < 1 || ifNoneMatch.length > 256)) {
    throw new BadRequestException('If-None-Match is invalid');
  }
}
