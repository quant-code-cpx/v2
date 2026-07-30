import { BadRequestException, Injectable } from '@nestjs/common';

import {
  MarketOverviewClient,
  type MarketConditionalRead,
} from '../../data-sync/clients/market-overview.client.js';
import type {
  MarketCalendarPage,
  MarketEquityMoneyFlowRankingPage,
  MarketEquityRankingPage,
  MarketIndexBarPage,
  MarketOverview,
  MarketSectorMoneyFlowRankingPage,
  MarketSectorStrengthPage,
  SwIndustryBarPage,
  SwIndustryConstituentPage,
  SwIndustryValuation,
} from '../../data-sync/contracts/market-overview.contract.js';
import type { SwIndustryPathDto } from '../industry/dto/sw-industry-path.dto.js';
import type {
  ListMarketEquityMoneyFlowRankingsBodyDto,
  ListMarketEquityRankingsBodyDto,
  ListMarketIndexBarsBodyDto,
  ListMarketSectorStrengthBodyDto,
  ListMarketSectorMoneyFlowRankingsBodyDto,
  ListSwIndustryBarsBodyDto,
  ListSwIndustryConstituentsBodyDto,
  GetSwIndustryValuationBodyDto,
  MarketIndexPathDto,
  MarketOverviewBodyDto,
  QueryMarketCalendarBodyDto,
} from './dto/market-overview.dto.js';

/** 编排市场首页、排行、日历和新增行业读取，不持有或临时拼接同步事实。 */
@Injectable()
export class MarketOverviewService {
  /** 注入唯一允许访问市场完整包内部读端的防腐 client。 */
  public constructor(private readonly client: MarketOverviewClient) {}

  /** 返回 latest 或精确交易日的单个完整首页 bundle。 */
  public getOverview(
    input: MarketOverviewBodyDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<MarketConditionalRead<MarketOverview>> {
    assertConditionalHeader(ifNoneMatch);
    return this.client.getOverview({ asOf: input.asOf, ifNoneMatch, requestId });
  }

  /** 返回固定指数身份的来源日线，并拒绝倒置或无界查询。 */
  public listIndexBars(
    path: MarketIndexPathDto,
    input: ListMarketIndexBarsBodyDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<MarketConditionalRead<MarketIndexBarPage>> {
    assertDateRange(input.start, input.end, 7_500);
    assertConditionalHeader(ifNoneMatch);
    return this.client.listIndexBars({
      indexId: path.indexId,
      period: input.period,
      start: input.start,
      end: input.end,
      cursor: input.cursor,
      limit: input.limit,
      ifNoneMatch,
      requestId,
    });
  }

  /** 返回冻结全市场横截面中的证券排行。 */
  public listEquityRankings(
    input: ListMarketEquityRankingsBodyDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<MarketConditionalRead<MarketEquityRankingPage>> {
    assertConditionalHeader(ifNoneMatch);
    return this.client.listEquityRankings({ ...input, ifNoneMatch, requestId });
  }

  /** 返回显式供应商订单规模方法学下的一侧证券资金流排行。 */
  public listEquityMoneyFlowRankings(
    input: ListMarketEquityMoneyFlowRankingsBodyDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<MarketConditionalRead<MarketEquityMoneyFlowRankingPage>> {
    assertConditionalHeader(ifNoneMatch);
    return this.client.listEquityMoneyFlowRankings({ ...input, ifNoneMatch, requestId });
  }

  /** 返回沪深交易日历及会话日程，并限制查询窗口。 */
  public queryCalendar(
    input: QueryMarketCalendarBodyDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<MarketConditionalRead<MarketCalendarPage>> {
    assertDateRange(input.start, input.end, 730);
    assertConditionalHeader(ifNoneMatch);
    return this.client.queryCalendar({ ...input, ifNoneMatch, requestId });
  }

  /** 返回一个分类体系和冻结窗口下的板块强弱 publication。 */
  public listSectorStrength(
    input: ListMarketSectorStrengthBodyDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<MarketConditionalRead<MarketSectorStrengthPage>> {
    assertConditionalHeader(ifNoneMatch);
    return this.client.listSectorStrength({ ...input, ifNoneMatch, requestId });
  }

  /** 返回一个东财分类体系的来源资金流排行，并保留供应商方法学边界。 */
  public listSectorMoneyFlowRankings(
    input: ListMarketSectorMoneyFlowRankingsBodyDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<MarketConditionalRead<MarketSectorMoneyFlowRankingPage>> {
    assertConditionalHeader(ifNoneMatch);
    return this.client.listSectorMoneyFlowRankings({ ...input, ifNoneMatch, requestId });
  }

  /** 返回申万行业已发布周期行情，不把东财行业或请求时聚合结果作为替代。 */
  public listSwIndustryBars(
    path: SwIndustryPathDto,
    input: ListSwIndustryBarsBodyDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<MarketConditionalRead<SwIndustryBarPage>> {
    assertDateRange(input.start, input.end, 7_500);
    assertConditionalHeader(ifNoneMatch);
    return this.client.listSwIndustryBars({
      code: path.code,
      period: input.period,
      start: input.start,
      end: input.end,
      cursor: input.cursor,
      limit: input.limit,
      ifNoneMatch,
      requestId,
    });
  }

  /** 返回申万正式成分页，并保持该 publication 自身日期与版本。 */
  public listSwIndustryConstituents(
    path: SwIndustryPathDto,
    input: ListSwIndustryConstituentsBodyDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<MarketConditionalRead<SwIndustryConstituentPage>> {
    assertConditionalHeader(ifNoneMatch);
    return this.client.listSwIndustryConstituents({
      code: path.code,
      asOf: input.asOf,
      cursor: input.cursor,
      limit: input.limit,
      ifNoneMatch,
      requestId,
    });
  }

  /** 返回单个申万节点逐字段可解释的估值，未报告指标保留真实空值。 */
  public getSwIndustryValuation(
    path: SwIndustryPathDto,
    input: GetSwIndustryValuationBodyDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<MarketConditionalRead<SwIndustryValuation>> {
    assertConditionalHeader(ifNoneMatch);
    return this.client.getSwIndustryValuation({
      code: path.code,
      asOf: input.asOf,
      ifNoneMatch,
      requestId,
    });
  }
}

/** 拒绝倒置或超过容量预算的日期窗口。 */
function assertDateRange(start: string, end: string, maximumDays: number): void {
  if (start > end) throw new BadRequestException('start must not be after end');
  const startTime = Date.parse(`${start}T00:00:00Z`);
  const endTime = Date.parse(`${end}T00:00:00Z`);
  const days = Math.floor((endTime - startTime) / 86_400_000) + 1;
  if (!Number.isSafeInteger(days) || days < 1 || days > maximumDays) {
    throw new BadRequestException('date range is too large');
  }
}

/** 限制条件请求头长度和换行，避免无界或非法值透传下游。 */
function assertConditionalHeader(ifNoneMatch: string | undefined): void {
  if (
    ifNoneMatch !== undefined &&
    (ifNoneMatch.length < 1 || ifNoneMatch.length > 256 || /[\r\n]/.test(ifNoneMatch))
  ) {
    throw new BadRequestException('If-None-Match is invalid');
  }
}
