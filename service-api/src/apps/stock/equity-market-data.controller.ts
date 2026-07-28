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
import { EquityPathDto } from './dto/equity-path.dto.js';
import { ListAdjustmentFactorsQueryDto } from './dto/list-adjustment-factors-query.dto.js';
import { ListCorporateActionsQueryDto } from './dto/list-corporate-actions-query.dto.js';
import { ListEquityBarsQueryDto } from './dto/list-equity-bars-query.dto.js';
import { EquityMarketDataService } from './equity-market-data.service.js';
import { writeConditionalResponse } from './equity-instrument.controller.js';

/** 表示已通过全局请求标识中间件的认证请求。 */
type CorrelatedAuthenticatedRequest = AuthenticatedRequest & { requestId: string };

/** 暴露个股行情、复权因子、公司行动与概况的公开 POST 路由。 */
@ApiTags('equity-market-data')
@ApiBearerAuth()
@Controller('equities')
export class EquityMarketDataController {
  /** 将公开请求交给应用服务，不直接访问同步数据库或 AKShare。 */
  public constructor(private readonly marketData: EquityMarketDataService) {}

  @Post(':exchange/:symbol/bars')
  @HttpCode(HttpStatus.OK)
  /** 返回上游直取的日、周或月行情，以及可选查询时复权结果。 */
  public async listBars(
    @Param() path: EquityPathDto,
    @Query() query: ListEquityBarsQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.marketData.listBars(path, query, ifNoneMatch, request.requestId);
    return writeConditionalResponse(response, result);
  }

  @Post(':exchange/:symbol/adjustment-factors')
  @HttpCode(HttpStatus.OK)
  /** 返回稀疏累计后复权因子序列。 */
  public async listAdjustmentFactors(
    @Param() path: EquityPathDto,
    @Query() query: ListAdjustmentFactorsQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.marketData.listAdjustmentFactors(
      path,
      query,
      ifNoneMatch,
      request.requestId,
    );
    return writeConditionalResponse(response, result);
  }

  @Post(':exchange/:symbol/corporate-actions')
  @HttpCode(HttpStatus.OK)
  /** 返回分红送转事件当前 revision。 */
  public async listCorporateActions(
    @Param() path: EquityPathDto,
    @Query() query: ListCorporateActionsQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.marketData.listCorporateActions(
      path,
      query,
      ifNoneMatch,
      request.requestId,
    );
    return writeConditionalResponse(response, result);
  }

  @Post(':exchange/:symbol/company-profile')
  @HttpCode(HttpStatus.OK)
  /** 返回当前已发布公司概况。 */
  public async getCompanyProfile(
    @Param() path: EquityPathDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.marketData.getCompanyProfile(path, ifNoneMatch, request.requestId);
    return writeConditionalResponse(response, result);
  }
}
