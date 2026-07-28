import { BadRequestException, Injectable } from '@nestjs/common';

import { EquityMarketDataClient } from '../../data-sync/clients/equity-market-data.client.js';
import type { ConditionalRead } from '../../data-sync/clients/equity-instrument.client.js';
import type {
  EquityAdjustmentFactorPage,
  EquityBarPage,
  EquityCompanyProfile,
  EquityCorporateActionPage,
} from '../../data-sync/contracts/equity-market-data.contract.js';
import type { EquityPathDto } from './dto/equity-path.dto.js';
import type { ListAdjustmentFactorsQueryDto } from './dto/list-adjustment-factors-query.dto.js';
import type { ListCorporateActionsQueryDto } from './dto/list-corporate-actions-query.dto.js';
import type { ListEquityBarsQueryDto } from './dto/list-equity-bars-query.dto.js';

/** 编排认证用户的个股行情、复权、事件与概况读取。 */
@Injectable()
export class EquityMarketDataService {
  /** 注入唯一允许访问同步服务的市场数据 client。 */
  public constructor(private readonly client: EquityMarketDataClient) {}

  /** 读取一个日、周或月行情窗口。 */
  public listBars(
    path: EquityPathDto,
    query: ListEquityBarsQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<ConditionalRead<EquityBarPage>> {
    assertDateRange(query.start, query.end);
    assertIfNoneMatch(ifNoneMatch);
    if (query.adjust === 'none' && query.adjustAsOf !== undefined) {
      throw new BadRequestException('adjustAsOf requires qfq or hfq');
    }
    return this.client.listBars({
      exchange: path.exchange,
      symbol: path.symbol,
      period: query.period,
      start: query.start,
      end: query.end,
      adjust: query.adjust,
      adjustAsOf: query.adjustAsOf,
      cursor: query.cursor,
      limit: query.limit,
      ifNoneMatch,
      requestId,
    });
  }

  /** 读取累计后复权因子。 */
  public listAdjustmentFactors(
    path: EquityPathDto,
    query: ListAdjustmentFactorsQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<ConditionalRead<EquityAdjustmentFactorPage>> {
    if (query.start !== undefined) assertDateRange(query.start, query.end);
    assertIfNoneMatch(ifNoneMatch);
    return this.client.listAdjustmentFactors({
      exchange: path.exchange,
      symbol: path.symbol,
      start: query.start,
      end: query.end,
      cursor: query.cursor,
      limit: query.limit,
      ifNoneMatch,
      requestId,
    });
  }

  /** 读取公司行动事件。 */
  public listCorporateActions(
    path: EquityPathDto,
    query: ListCorporateActionsQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<ConditionalRead<EquityCorporateActionPage>> {
    if (query.start !== undefined && query.end !== undefined) {
      assertDateRange(query.start, query.end);
    }
    assertIfNoneMatch(ifNoneMatch);
    return this.client.listCorporateActions({
      exchange: path.exchange,
      symbol: path.symbol,
      start: query.start,
      end: query.end,
      cursor: query.cursor,
      limit: query.limit,
      ifNoneMatch,
      requestId,
    });
  }

  /** 读取当前公司概况。 */
  public getCompanyProfile(
    path: EquityPathDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<ConditionalRead<EquityCompanyProfile>> {
    assertIfNoneMatch(ifNoneMatch);
    return this.client.getCompanyProfile({
      exchange: path.exchange,
      symbol: path.symbol,
      ifNoneMatch,
      requestId,
    });
  }
}

/** 拒绝倒置的包含端日期窗口。 */
function assertDateRange(start: string, end: string): void {
  if (start > end) throw new BadRequestException('start must not be after end');
}

/** 限制条件请求头长度，避免无界透传。 */
function assertIfNoneMatch(ifNoneMatch: string | undefined): void {
  if (ifNoneMatch !== undefined && (ifNoneMatch.length < 1 || ifNoneMatch.length > 256)) {
    throw new BadRequestException('If-None-Match is invalid');
  }
}
