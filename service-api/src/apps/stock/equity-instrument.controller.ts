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
import type { ConditionalRead } from '../../data-sync/clients/equity-instrument.client.js';
import { EquityPathDto } from './dto/equity-path.dto.js';
import { EquityTemporalQueryDto } from './dto/equity-temporal-query.dto.js';
import { ListEquitiesQueryDto } from './dto/list-equities-query.dto.js';
import { ListListingStatusHistoryQueryDto } from './dto/list-listing-status-history-query.dto.js';
import { EquityInstrumentService } from './equity-instrument.service.js';

/** 表示已经经过全局请求标识中间件的认证请求。 */
type CorrelatedAuthenticatedRequest = AuthenticatedRequest & { requestId: string };

type VersionedBody = { dataVersion: string | null };

/** 暴露给已认证用户的证券目录、详情和上市生命周期读取路由。 */
@ApiTags('equity-instruments')
@ApiBearerAuth()
@Controller('equities')
export class EquityInstrumentController {
  /** 将公开请求交给应用服务，不直接访问同步服务或数据库。 */
  public constructor(private readonly equities: EquityInstrumentService) {}

  @Post()
  @HttpCode(HttpStatus.OK)
  /** 返回一页证券目录，或在 `ETag` 命中时返回无响应体的 204。 */
  public async list(
    @Query() query: ListEquitiesQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.equities.listEquities(query, ifNoneMatch, request.requestId);
    return writeConditionalResponse(response, result);
  }

  @Post(':exchange/:symbol/listing-status-history')
  @HttpCode(HttpStatus.OK)
  /** 返回按身份适用日期解析的一只证券上市生命周期历史。 */
  public async listListingStatusHistory(
    @Param() path: EquityPathDto,
    @Query() query: ListListingStatusHistoryQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.equities.listListingStatusHistory(
      path,
      query,
      ifNoneMatch,
      request.requestId,
    );
    return writeConditionalResponse(response, result);
  }

  @Post(':exchange/:symbol')
  @HttpCode(HttpStatus.OK)
  /** 返回按业务时间和知识时间唯一解析的一只证券。 */
  public async get(
    @Param() path: EquityPathDto,
    @Query() query: EquityTemporalQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.equities.getEquity(path, query, ifNoneMatch, request.requestId);
    return writeConditionalResponse(response, result);
  }
}

/** 设置可复验缓存头、数据版本，并把下游 304 映射为合法的公开 POST 204 响应。 */
export function writeConditionalResponse<T extends VersionedBody>(
  response: Response,
  result: ConditionalRead<T>,
): T | undefined {
  if (result.etag !== undefined) response.setHeader('ETag', result.etag);
  response.setHeader('Cache-Control', 'private, max-age=0, must-revalidate');
  if (result.status === 304) {
    response.status(204).send();
    return undefined;
  }
  if (result.body.dataVersion !== null) {
    response.setHeader('X-Data-Version', result.body.dataVersion);
  }
  return result.body;
}
