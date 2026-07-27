import { Controller, Get, Headers, Param, Query, Res } from '@nestjs/common';
import { ApiBearerAuth, ApiTags } from '@nestjs/swagger';

import type { Response } from 'express';

import { ListSectorBarsQueryDto } from './dto/list-sector-bars-query.dto.js';
import { ListSectorMembershipQueryDto } from './dto/list-sector-membership-query.dto.js';
import { ListSectorsQueryDto } from './dto/list-sectors-query.dto.js';
import { SectorPathDto } from './dto/sector-path.dto.js';
import { SectorMarketDataService } from './sector-market-data.service.js';

/** 暴露给已认证用户的板块目录与独立物理周期 K 线读取路由。 */
@ApiTags('market-sectors')
@ApiBearerAuth()
@Controller('market/sectors')
export class SectorMarketDataController {
  /** 将 HTTP 查询交给板块应用服务，不让控制器直接调用下游服务。 */
  public constructor(private readonly sectors: SectorMarketDataService) {}

  @Get()
  /** 返回一个板块目录页，或在 ETag 命中时转发 304。 */
  public async list(
    @Query() query: ListSectorsQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.sectors.listSectors(query, ifNoneMatch);
    return writeConditionalResponse(response, result);
  }

  @Get(':scheme/:sectorCode/bars')
  /** 返回板块日、周或月上游行情页，绝不由 API 根据日线重新聚合。 */
  public async listBars(
    @Param() path: SectorPathDto,
    @Query() query: ListSectorBarsQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.sectors.listBars(path, query, ifNoneMatch);
    return writeConditionalResponse(response, result);
  }

  @Get(':scheme/:sectorCode/constituents')
  /** 返回固定 release 中一板块的 verified 成分观测，不伪造真实调入调出日期。 */
  public async listConstituents(
    @Param() path: SectorPathDto,
    @Query() query: ListSectorMembershipQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.sectors.listConstituents(path, query, ifNoneMatch);
    return writeConditionalResponse(response, result);
  }
}

/** 设置公开读取响应的可复验缓存头，并在下游 304 时不渲染响应体。 */
function writeConditionalResponse(
  response: Response,
  result:
    | { status: 304; etag: string | undefined }
    | { status: 200; etag: string | undefined; body: unknown },
): unknown {
  if (result.etag !== undefined) response.setHeader('ETag', result.etag);
  response.setHeader('Cache-Control', 'private, max-age=0, must-revalidate');
  if (result.status === 304) {
    response.status(304).send();
    return undefined;
  }
  return result.body;
}
