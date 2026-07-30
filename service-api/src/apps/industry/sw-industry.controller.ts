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
import { GetSwIndustryQueryDto } from './dto/get-sw-industry-query.dto.js';
import { ListSwIndustriesQueryDto } from './dto/list-sw-industries-query.dto.js';
import { ListSwValuationsQueryDto } from './dto/list-sw-valuations-query.dto.js';
import { SwIndustryPathDto } from './dto/sw-industry-path.dto.js';
import { SwIndustryService } from './sw-industry.service.js';
import type { SwUpstreamResponse } from '../../data-sync/clients/sw-sector.client.js';

type CorrelatedAuthenticatedRequest = AuthenticatedRequest & { requestId: string };

/** 暴露已认证用户可读的申万 taxonomy、父级闭包和估值 POST API。 */
@ApiTags('market-sw-industries')
@ApiBearerAuth()
@Controller('market/industries/sw')
export class SwIndustryController {
  /** 将公开 HTTP 输入交给申万应用服务，不直接访问同步服务。 */
  public constructor(private readonly industries: SwIndustryService) {}

  @Post('list')
  @HttpCode(HttpStatus.OK)
  /** 返回冻结 taxonomy 页，ETag 命中时以合法 POST 204 表达无响应体。 */
  public async list(
    @Query() query: ListSwIndustriesQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    return writeConditionalResponse(
      response,
      await this.industries.listIndustries(query, ifNoneMatch, request.requestId),
    );
  }

  @Post('valuations')
  @HttpCode(HttpStatus.OK)
  /** 返回供应商观察估值页，不把页面展示值宣称为官方最终值。 */
  public async listValuations(
    @Query() query: ListSwValuationsQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    return writeConditionalResponse(
      response,
      await this.industries.listValuations(query, ifNoneMatch, request.requestId),
    );
  }

  @Post(':code')
  @HttpCode(HttpStatus.OK)
  /** 返回一个申万节点及同一 dataVersion 中根到直接父级闭包。 */
  public async get(
    @Param() path: SwIndustryPathDto,
    @Query() query: GetSwIndustryQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    return writeConditionalResponse(
      response,
      await this.industries.getIndustry(path, query, ifNoneMatch, request.requestId),
    );
  }
}

/** 复制同步服务 ETag，并把内部 GET 304 映射为公开 POST 204。 */
function writeConditionalResponse<T>(
  response: Response,
  result: SwUpstreamResponse<T>,
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
