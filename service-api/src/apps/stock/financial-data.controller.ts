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
import type { FinancialConditionalRead } from '../../data-sync/clients/financial-data.client.js';
import { EquityPathDto } from './dto/equity-path.dto.js';
import { FinancialReportPathDto } from './dto/financial-report-path.dto.js';
import { GetFinancialReportQueryDto } from './dto/get-financial-report-query.dto.js';
import { ListFinancialMetricsQueryDto } from './dto/list-financial-metrics-query.dto.js';
import { ListFinancialReportsQueryDto } from './dto/list-financial-reports-query.dto.js';
import { ListValuationsQueryDto } from './dto/list-valuations-query.dto.js';
import { FinancialDataService } from './financial-data.service.js';

/** 表示已通过全局鉴权和请求标识中间件的请求。 */
type CorrelatedAuthenticatedRequest = AuthenticatedRequest & { requestId: string };

/** 约束条件响应可传播消费者 dataVersion。 */
type VersionedBody = { dataVersion: string };

/** 暴露财务报表、指标与估值的认证公开 POST 路由。 */
@ApiTags('equity-financial-data')
@ApiBearerAuth()
@Controller('equities')
export class FinancialDataController {
  /** 将 HTTP 请求交给应用服务，不直接访问同步数据库或 provider。 */
  public constructor(private readonly financialData: FinancialDataService) {}

  @Post(':exchange/:symbol/financial-reports')
  @HttpCode(HttpStatus.OK)
  /** 返回一个方法学的报表页，或在 `ETag` 命中时返回无响应体 204。 */
  public async listReports(
    @Param() path: EquityPathDto,
    @Query() query: ListFinancialReportsQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.financialData.listReports(
      path,
      query,
      ifNoneMatch,
      request.requestId,
    );
    return writeFinancialConditionalResponse(response, result);
  }

  @Post(':exchange/:symbol/financial-reports/:reportRef')
  @HttpCode(HttpStatus.OK)
  /** 返回一份公开报表的治理行项目页。 */
  public async getReport(
    @Param() path: FinancialReportPathDto,
    @Query() query: GetFinancialReportQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.financialData.getReport(path, query, ifNoneMatch, request.requestId);
    return writeFinancialConditionalResponse(response, result);
  }

  @Post(':exchange/:symbol/financial-metrics')
  @HttpCode(HttpStatus.OK)
  /** 返回显式供应商直报或平台派生方法学的指标页。 */
  public async listMetrics(
    @Param() path: EquityPathDto,
    @Query() query: ListFinancialMetricsQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.financialData.listMetrics(
      path,
      query,
      ifNoneMatch,
      request.requestId,
    );
    return writeFinancialConditionalResponse(response, result);
  }

  @Post(':exchange/:symbol/valuations')
  @HttpCode(HttpStatus.OK)
  /** 返回显式估值方法学的历史观察页。 */
  public async listValuations(
    @Param() path: EquityPathDto,
    @Query() query: ListValuationsQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.financialData.listValuations(
      path,
      query,
      ifNoneMatch,
      request.requestId,
    );
    return writeFinancialConditionalResponse(response, result);
  }
}

/** 设置财务可复验缓存头，并把内部 304 映射为公开 POST 204。 */
export function writeFinancialConditionalResponse<T extends VersionedBody>(
  response: Response,
  result: FinancialConditionalRead<T>,
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
