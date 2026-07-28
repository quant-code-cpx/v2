import {
  Controller,
  Headers,
  HttpCode,
  HttpStatus,
  Param,
  Post,
  Query,
  Req,
  Res,
} from '@nestjs/common';
import { ApiBearerAuth, ApiTags } from '@nestjs/swagger';

import type { Response } from 'express';

import type { AuthenticatedRequest } from '../../common/models/auth-context.js';
import type { MoneyFlowConditionalRead } from '../../data-sync/clients/money-flow.client.js';
import {
  EquityMoneyFlowPathDto,
  ListMoneyFlowDailyQueryDto,
  ListMoneyFlowMethodologiesQueryDto,
  ListMoneyFlowRankingQueryDto,
  MarketMoneyFlowPathDto,
  MoneyFlowMethodologyPathDto,
  SectorMoneyFlowPathDto,
} from './dto/money-flow.dto.js';
import { MoneyFlowService } from './money-flow.service.js';

type CorrelatedAuthenticatedRequest = AuthenticatedRequest & { requestId: string };
type VersionedBody = { dataVersion: string };

/** 暴露 0016 的五条认证公开 POST 路由。 */
@ApiTags('market-money-flow')
@ApiBearerAuth()
@Controller('market/money-flow')
export class MoneyFlowController {
  /** 将公开 HTTP 请求交给应用服务，不直接访问同步库或 provider。 */
  public constructor(private readonly moneyFlow: MoneyFlowService) {}

  @Post('methodologies')
  @HttpCode(HttpStatus.OK)
  /** 返回 API 可见方法学目录，或在 ETag 命中时返回 204。 */
  public async listMethodologies(
    @Query() query: ListMoneyFlowMethodologiesQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.moneyFlow.listMethodologies(query, ifNoneMatch, request.requestId);
    return writeMoneyFlowConditionalResponse(response, result);
  }

  @Post('methodologies/:methodologyId/daily-series/equities/:exchange/:symbol')
  @HttpCode(HttpStatus.OK)
  /** 返回一个证券来源日序列，历史代码按每个事实日解析。 */
  public async listEquityDaily(
    @Param() path: EquityMoneyFlowPathDto,
    @Query() query: ListMoneyFlowDailyQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.moneyFlow.listEquityDaily(
      path,
      query,
      ifNoneMatch,
      request.requestId,
    );
    return writeMoneyFlowConditionalResponse(response, result);
  }

  @Post('methodologies/:methodologyId/daily-series/sectors/:scheme/:sectorCode')
  @HttpCode(HttpStatus.OK)
  /** 返回一个上游板块来源日序列。 */
  public async listSectorDaily(
    @Param() path: SectorMoneyFlowPathDto,
    @Query() query: ListMoneyFlowDailyQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.moneyFlow.listSectorDaily(
      path,
      query,
      ifNoneMatch,
      request.requestId,
    );
    return writeMoneyFlowConditionalResponse(response, result);
  }

  @Post('methodologies/:methodologyId/daily-series/markets/:marketCode')
  @HttpCode(HttpStatus.OK)
  /** 返回一个来源报告的市场 scope 日序列。 */
  public async listMarketDaily(
    @Param() path: MarketMoneyFlowPathDto,
    @Query() query: ListMoneyFlowDailyQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.moneyFlow.listMarketDaily(
      path,
      query,
      ifNoneMatch,
      request.requestId,
    );
    return writeMoneyFlowConditionalResponse(response, result);
  }

  @Post('methodologies/:methodologyId/supplier-rankings')
  @HttpCode(HttpStatus.OK)
  /** 返回 exact 或 latest 的不可变供应商排行，不重算位置。 */
  public async listRanking(
    @Param() path: MoneyFlowMethodologyPathDto,
    @Query() query: ListMoneyFlowRankingQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.moneyFlow.listRanking(path, query, ifNoneMatch, request.requestId);
    return writeMoneyFlowConditionalResponse(response, result);
  }
}

/** 设置可复验缓存头，并把内部 GET 304 映射为公开 POST 204。 */
export function writeMoneyFlowConditionalResponse<T extends VersionedBody>(
  response: Response,
  result: MoneyFlowConditionalRead<T>,
): T | undefined {
  response.setHeader('ETag', result.etag);
  response.setHeader('Cache-Control', 'private, max-age=0, must-revalidate');
  response.setHeader('X-Data-Version', result.dataVersion);
  if (result.status === 304) {
    response.status(204).send();
    return undefined;
  }
  return result.body;
}
