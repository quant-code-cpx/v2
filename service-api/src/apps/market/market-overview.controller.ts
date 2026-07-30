import {
  Body,
  Controller,
  Headers,
  HttpCode,
  HttpStatus,
  Param,
  Post,
  Req,
  Res,
} from '@nestjs/common';
import { ApiBearerAuth, ApiTags } from '@nestjs/swagger';

import type { Response } from 'express';

import type { AuthenticatedRequest } from '../../common/models/auth-context.js';
import type { MarketConditionalRead } from '../../data-sync/clients/market-overview.client.js';
import { SwIndustryPathDto } from '../industry/dto/sw-industry-path.dto.js';
import {
  GetSwIndustryValuationBodyDto,
  ListMarketEquityMoneyFlowRankingsBodyDto,
  ListMarketEquityRankingsBodyDto,
  ListMarketIndexBarsBodyDto,
  ListMarketSectorMoneyFlowRankingsBodyDto,
  ListMarketSectorStrengthBodyDto,
  ListSwIndustryBarsBodyDto,
  ListSwIndustryConstituentsBodyDto,
  MarketIndexPathDto,
  MarketOverviewBodyDto,
  QueryMarketCalendarBodyDto,
} from './dto/market-overview.dto.js';
import { MarketOverviewService } from './market-overview.service.js';

/** 表示已经过全局关联标识中间件的认证请求。 */
type CorrelatedAuthenticatedRequest = AuthenticatedRequest & { requestId: string };

/** 暴露市场完整包、排行、日历以及新增行业数据的公开 POST 读取。 */
@ApiTags('market-overview')
@ApiBearerAuth()
@Controller('market')
export class MarketOverviewController {
  /** 将公开请求交给市场应用服务，不直接访问同步数据库或供应商。 */
  public constructor(private readonly market: MarketOverviewService) {}

  @Post('overview')
  @HttpCode(HttpStatus.OK)
  /** 返回一个 latest 或精确交易日的 complete bundle，绝不读时拼接不同版本。 */
  public async getOverview(
    @Body() body: MarketOverviewBodyDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    return writeMarketConditionalResponse(
      response,
      await this.market.getOverview(body, ifNoneMatch, request.requestId),
    );
  }

  @Post('indices/:indexId/bars')
  @HttpCode(HttpStatus.OK)
  /** 返回固定指数身份的来源日 K 线，不使用指数成分观察伪造行情。 */
  public async listIndexBars(
    @Param() path: MarketIndexPathDto,
    @Body() body: ListMarketIndexBarsBodyDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    return writeMarketConditionalResponse(
      response,
      await this.market.listIndexBars(path, body, ifNoneMatch, request.requestId),
    );
  }

  @Post('equities/rankings')
  @HttpCode(HttpStatus.OK)
  /** 返回冻结全市场股票横截面的涨跌、成交额或换手率排行。 */
  public async listEquityRankings(
    @Body() body: ListMarketEquityRankingsBodyDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    return writeMarketConditionalResponse(
      response,
      await this.market.listEquityRankings(body, ifNoneMatch, request.requestId),
    );
  }

  @Post('money-flow/equity-rankings')
  @HttpCode(HttpStatus.OK)
  /** 返回显式 Tushare 订单规模方法学下的流入或流出股票排行。 */
  public async listEquityMoneyFlowRankings(
    @Body() body: ListMarketEquityMoneyFlowRankingsBodyDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    return writeMarketConditionalResponse(
      response,
      await this.market.listEquityMoneyFlowRankings(body, ifNoneMatch, request.requestId),
    );
  }

  @Post('calendar/query')
  @HttpCode(HttpStatus.OK)
  /** 返回沪深交易日历和会话日程，避免 Web 猜测交易状态。 */
  public async queryCalendar(
    @Body() body: QueryMarketCalendarBodyDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    return writeMarketConditionalResponse(
      response,
      await this.market.queryCalendar(body, ifNoneMatch, request.requestId),
    );
  }

  @Post('sectors/strength')
  @HttpCode(HttpStatus.OK)
  /** 返回一个板块体系内已独立发布的强弱与持续性排行。 */
  public async listSectorStrength(
    @Body() body: ListMarketSectorStrengthBodyDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    return writeMarketConditionalResponse(
      response,
      await this.market.listSectorStrength(body, ifNoneMatch, request.requestId),
    );
  }

  @Post('sectors/money-flow-rankings')
  @HttpCode(HttpStatus.OK)
  /** 返回东财来源资金流排行，禁止使用涨跌幅排行冒充资金方向。 */
  public async listSectorMoneyFlowRankings(
    @Body() body: ListMarketSectorMoneyFlowRankingsBodyDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    return writeMarketConditionalResponse(
      response,
      await this.market.listSectorMoneyFlowRankings(body, ifNoneMatch, request.requestId),
    );
  }

  @Post('industries/sw/:code/bars')
  @HttpCode(HttpStatus.OK)
  /** 返回申万行业来源日线与逐字段方法学，不映射东财同名板块。 */
  public async listSwIndustryBars(
    @Param() path: SwIndustryPathDto,
    @Body() body: ListSwIndustryBarsBodyDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    return writeMarketConditionalResponse(
      response,
      await this.market.listSwIndustryBars(path, body, ifNoneMatch, request.requestId),
    );
  }

  @Post('industries/sw/:code/constituents')
  @HttpCode(HttpStatus.OK)
  /** 返回申万正式成分页，不把观察区间描述成官方调入调出日期。 */
  public async listSwIndustryConstituents(
    @Param() path: SwIndustryPathDto,
    @Body() body: ListSwIndustryConstituentsBodyDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    return writeMarketConditionalResponse(
      response,
      await this.market.listSwIndustryConstituents(path, body, ifNoneMatch, request.requestId),
    );
  }

  @Post('industries/sw/:code/valuation')
  @HttpCode(HttpStatus.OK)
  /** 返回单个申万节点逐字段可解释的估值，不要求 Web 扫描整个层级。 */
  public async getSwIndustryValuation(
    @Param() path: SwIndustryPathDto,
    @Body() body: GetSwIndustryValuationBodyDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    return writeMarketConditionalResponse(
      response,
      await this.market.getSwIndustryValuation(path, body, ifNoneMatch, request.requestId),
    );
  }
}

/** 复制 publication 缓存元数据，并把内部 GET 304 映射为公开 POST 204。 */
export function writeMarketConditionalResponse<T extends { dataVersion: string }>(
  response: Response,
  result: MarketConditionalRead<T>,
): T | undefined {
  response.setHeader('ETag', result.etag);
  response.setHeader('X-Data-Version', result.dataVersion);
  response.setHeader('Cache-Control', 'private, max-age=0, must-revalidate');
  if (result.status === 304) {
    response.status(HttpStatus.NO_CONTENT).send();
    return undefined;
  }
  return result.body;
}
