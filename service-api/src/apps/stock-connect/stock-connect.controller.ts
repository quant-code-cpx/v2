import { Body, Controller, Headers, HttpCode, HttpStatus, Post, Req, Res } from '@nestjs/common';
import { ApiBearerAuth, ApiTags } from '@nestjs/swagger';

import type { Response } from 'express';

import type { AuthenticatedRequest } from '../../common/models/auth-context.js';
import {
  StockConnectActiveSecurityQueryDto,
  StockConnectChannelQueryDto,
  StockConnectOverviewQueryDto,
  StockConnectReadinessQueryDto,
  StockConnectSecurityContextQueryDto,
} from './dto/stock-connect-query.dto.js';
import { StockConnectRateLimitService } from './stock-connect-rate-limit.service.js';
import { StockConnectService, type StockConnectConditionalRead } from './stock-connect.service.js';

/** 表示全局鉴权和请求标识中间件处理后的公开请求。 */
type CorrelatedAuthenticatedRequest = AuthenticatedRequest & { requestId: string };

/** 暴露五条认证、POST-only 的沪深港通公开查询路由。 */
@ApiTags('market-stock-connect')
@ApiBearerAuth()
@Controller('market/stock-connect')
export class StockConnectController {
  /** 注入业务编排与分布式短期安全限流。 */
  public constructor(
    private readonly stockConnect: StockConnectService,
    private readonly rateLimit: StockConnectRateLimitService,
  ) {}

  /** 返回所选通道最后一个共同完成 publication 的总览。 */
  @Post('overview/query')
  @HttpCode(HttpStatus.OK)
  public async overview(
    @Body() query: StockConnectOverviewQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    await this.rateLimit.assertAllowed(request.user.userId, 'OVERVIEW');
    const result = await this.stockConnect.overview(query, ifNoneMatch, request.requestId);
    return writeStockConnectConditionalResponse(response, result);
  }

  /** 返回候选交易日逐通道 readiness 及其独立证据版本。 */
  @Post('readiness/query')
  @HttpCode(HttpStatus.OK)
  public async readiness(
    @Body() query: StockConnectReadinessQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    await this.rateLimit.assertAllowed(request.user.userId, 'READINESS');
    const result = await this.stockConnect.readiness(query, ifNoneMatch, request.requestId);
    return writeStockConnectConditionalResponse(response, result);
  }

  /** 返回一条通道的真实日终统计、额度、状态和趋势。 */
  @Post('channels/query')
  @HttpCode(HttpStatus.OK)
  public async channel(
    @Body() query: StockConnectChannelQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    await this.rateLimit.assertAllowed(request.user.userId, 'CHANNEL');
    const result = await this.stockConnect.channel(query, ifNoneMatch, request.requestId);
    return writeStockConnectConditionalResponse(response, result);
  }

  /** 返回官方来源活跃证券榜或仅在该榜内可用的净额排序。 */
  @Post('active-securities/query')
  @HttpCode(HttpStatus.OK)
  public async activeSecurities(
    @Body() query: StockConnectActiveSecurityQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    await this.rateLimit.assertAllowed(request.user.userId, 'ACTIVE_SECURITIES');
    const result = await this.stockConnect.activeSecurities(query, ifNoneMatch, request.requestId);
    return writeStockConnectConditionalResponse(response, result);
  }

  /** 返回稳定证券引用在互联互通通道内的历史表现。 */
  @Post('securities/context/query')
  @HttpCode(HttpStatus.OK)
  public async securityContext(
    @Body() query: StockConnectSecurityContextQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    await this.rateLimit.assertAllowed(request.user.userId, 'SECURITY_CONTEXT');
    const result = await this.stockConnect.securityContext(query, ifNoneMatch, request.requestId);
    return writeStockConnectConditionalResponse(response, result);
  }
}

/** 设置私有缓存与不可变版本头，并把条件命中映射为公开 POST 204。 */
export function writeStockConnectConditionalResponse<T>(
  response: Response,
  result: StockConnectConditionalRead<T>,
): T | undefined {
  response.setHeader('Cache-Control', 'private, no-cache');
  response.setHeader('X-Data-Version', result.dataVersion);
  response.setHeader('ETag', result.etag);
  if (result.status === 204) {
    response.status(HttpStatus.NO_CONTENT).send();
    return undefined;
  }
  return result.body;
}
