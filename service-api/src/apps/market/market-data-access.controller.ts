import { Body, Controller, HttpCode, HttpStatus, Post, Req } from '@nestjs/common';
import { ApiBearerAuth, ApiTags } from '@nestjs/swagger';

import type { AuthenticatedRequest } from '../../common/models/auth-context.js';
import type { MarketDataQueryResponse } from '../../data-sync/contracts/market-data-access.contract.js';
import { MarketDataAccessService } from './market-data-access.service.js';

/** 表示经过全局鉴权和请求标识中间件处理的公开 API 请求。 */
type CorrelatedAuthenticatedRequest = AuthenticatedRequest & { requestId: string };

/** 提供 P0/P1 typed market-data 的统一公开 POST 读取入口。 */
@ApiTags('market-data')
@ApiBearerAuth()
@Controller('market-data')
export class MarketDataAccessController {
  /** 将请求交给应用服务，Controller 不直接访问数据库或第三方来源。 */
  public constructor(private readonly marketData: MarketDataAccessService) {}

  @Post('query')
  @HttpCode(HttpStatus.OK)
  /** 返回一个不可变发布页，或者在来源暂不可用时返回带状态的空 records。 */
  public query(
    @Body() body: unknown,
    @Req() request: CorrelatedAuthenticatedRequest,
  ): Promise<MarketDataQueryResponse> {
    return this.marketData.query(body, request.requestId);
  }
}
