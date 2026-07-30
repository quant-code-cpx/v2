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
import type { EquityWorkspaceConditionalRead } from '../../data-sync/clients/equity-workspace.client.js';
import { EquityPathDto } from './dto/equity-path.dto.js';
import { EquityWorkspaceService } from './equity-workspace.service.js';

/** 表示已通过全局认证和请求标识中间件的股票中心请求。 */
type CorrelatedAuthenticatedRequest = AuthenticatedRequest & { requestId: string };

/** 暴露统一证券发现、事件和数据状态的公开 POST 路由。 */
@ApiTags('equity-workspace')
@ApiBearerAuth()
@Controller('equities')
export class EquityWorkspaceController {
  /** 将 HTTP 边界输入交给应用服务，不直接访问同步服务或数据库。 */
  public constructor(private readonly workspace: EquityWorkspaceService) {}

  @Post('search')
  @HttpCode(HttpStatus.OK)
  /** 在单一 EOD publication 上执行证券发现。 */
  public async search(
    @Body() body: unknown,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.workspace.search(
      body,
      ifNoneMatch,
      request.requestId,
      request.user.userId,
    );
    return writeWorkspaceResponse(response, result);
  }

  @Post(':exchange/:symbol/events/search')
  @HttpCode(HttpStatus.OK)
  /** 按公开证券身份读取统一公司与交易事件。 */
  public async searchEvents(
    @Param() path: EquityPathDto,
    @Body() body: unknown,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.workspace.searchEvents(
      path,
      body,
      ifNoneMatch,
      request.requestId,
      request.user.userId,
    );
    return writeWorkspaceResponse(response, result);
  }

  @Post(':exchange/:symbol/data-status')
  @HttpCode(HttpStatus.OK)
  /** 返回详情数据集独立状态，不返回事实记录。 */
  public async getDataStatus(
    @Param() path: EquityPathDto,
    @Body() body: unknown,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.workspace.getDataStatus(
      path,
      body,
      ifNoneMatch,
      request.requestId,
      request.user.userId,
    );
    return writeWorkspaceResponse(response, result);
  }
}

/** 写入条件缓存头，并把内部 304 映射为合法的公开 POST 204。 */
function writeWorkspaceResponse<T>(
  response: Response,
  result: EquityWorkspaceConditionalRead<T>,
): T | undefined {
  if (result.etag !== undefined) response.setHeader('ETag', result.etag);
  if (result.dataVersion !== undefined) {
    response.setHeader('X-Data-Version', result.dataVersion);
  }
  if (result.status === 304) {
    response.setHeader('Cache-Control', 'private, max-age=0, must-revalidate');
    response.status(HttpStatus.NO_CONTENT).send();
    return undefined;
  }
  response.setHeader(
    'Cache-Control',
    isUnavailableEnvelope(result.body)
      ? 'private, max-age=30, must-revalidate'
      : 'private, max-age=0, must-revalidate',
  );
  return result.body;
}

/** 识别明确无 publication 的 envelope，允许浏览器短时抑制重复刷新。 */
function isUnavailableEnvelope(value: unknown): boolean {
  return (
    typeof value === 'object' &&
    value !== null &&
    'availability' in value &&
    value.availability === 'UNAVAILABLE'
  );
}
