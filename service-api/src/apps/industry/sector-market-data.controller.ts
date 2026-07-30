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
import { ListSectorBarsQueryDto } from './dto/list-sector-bars-query.dto.js';
import { GetSectorEodSnapshotQueryDto } from './dto/get-sector-eod-snapshot-query.dto.js';
import { ListSectorEodSnapshotsQueryDto } from './dto/list-sector-eod-snapshots-query.dto.js';
import { ListSectorMembershipQueryDto } from './dto/list-sector-membership-query.dto.js';
import { ListSectorsQueryDto } from './dto/list-sectors-query.dto.js';
import { SectorPathDto } from './dto/sector-path.dto.js';
import { SectorMarketDataService } from './sector-market-data.service.js';

/** 表示已经过全局关联标识中间件的认证请求。 */
type CorrelatedAuthenticatedRequest = AuthenticatedRequest & { requestId: string };

/** 暴露给已认证用户的板块目录与独立物理周期 K 线读取路由。 */
@ApiTags('market-sectors')
@ApiBearerAuth()
@Controller('market/sectors')
export class SectorMarketDataController {
  /** 将 HTTP 查询交给板块应用服务，不让控制器直接调用下游服务。 */
  public constructor(private readonly sectors: SectorMarketDataService) {}

  @Post('eod-snapshots')
  @HttpCode(HttpStatus.OK)
  /** 返回一个不可变 EOD 横截面的动态排行页，或在 `ETag` 命中时返回无响应体的 204。 */
  public async listEodSnapshots(
    @Query() query: ListSectorEodSnapshotsQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.sectors.listEodSnapshots(query, ifNoneMatch, request.requestId);
    return writeConditionalResponse(response, result);
  }

  @Post()
  @HttpCode(HttpStatus.OK)
  /** 返回一个板块目录页，或在 `ETag` 命中时返回无响应体的 204。 */
  public async list(
    @Query() query: ListSectorsQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.sectors.listSectors(query, ifNoneMatch, request.requestId);
    return writeConditionalResponse(response, result);
  }

  @Post(':scheme/:sectorCode/bars')
  @HttpCode(HttpStatus.OK)
  /** 返回板块日、周或月上游行情页，绝不由 API 根据日线重新聚合。 */
  public async listBars(
    @Param() path: SectorPathDto,
    @Query() query: ListSectorBarsQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.sectors.listBars(path, query, ifNoneMatch, request.requestId);
    return writeConditionalResponse(response, result);
  }

  @Post(':scheme/:sectorCode/eod-snapshot')
  @HttpCode(HttpStatus.OK)
  /** 返回单板块 latest 或精确交易日 EOD 报价，不以旧日期伪造指定日期结果。 */
  public async getEodSnapshot(
    @Param() path: SectorPathDto,
    @Query() query: GetSectorEodSnapshotQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.sectors.getEodSnapshot(path, query, ifNoneMatch, request.requestId);
    return writeConditionalResponse(response, result);
  }

  @Post(':scheme/:sectorCode/constituents')
  @HttpCode(HttpStatus.OK)
  /** 返回固定 release 中一板块的 verified 成分观测，不伪造真实调入调出日期。 */
  public async listConstituents(
    @Param() path: SectorPathDto,
    @Query() query: ListSectorMembershipQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.sectors.listConstituents(path, query, ifNoneMatch, request.requestId);
    return writeConditionalResponse(response, result);
  }
}

/** 设置公开读取响应的可复验缓存头，并把下游 304 映射为合法的公开 POST 204 响应。 */
function writeConditionalResponse(
  response: Response,
  result:
    | { status: 304; etag: string; dataVersion: string }
    | { status: 200; etag: string; dataVersion: string; body: unknown },
): unknown {
  response.setHeader('ETag', result.etag);
  response.setHeader('X-Data-Version', result.dataVersion);
  response.setHeader('Cache-Control', 'private, max-age=0, must-revalidate');
  if (result.status === 304) {
    response.status(204).send();
    return undefined;
  }
  return result.body;
}
