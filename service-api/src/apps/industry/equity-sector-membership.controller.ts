import { Controller, Headers, HttpCode, HttpStatus, Param, Post, Query, Res } from '@nestjs/common';
import { ApiBearerAuth, ApiTags } from '@nestjs/swagger';

import type { Response } from 'express';

import { EquityMembershipPathDto } from './dto/equity-membership-path.dto.js';
import { ListEquitySectorsQueryDto } from './dto/list-equity-sectors-query.dto.js';
import { SectorMarketDataService } from './sector-market-data.service.js';

/** 暴露给已认证用户的证券到板块观察反向读取，不新增本地 membership 权威存储。 */
@ApiTags('market-sector-membership')
@ApiBearerAuth()
@Controller('market/equities')
export class EquitySectorMembershipController {
  /** 将路径和查询交给板块应用服务，控制器不直接调用同步服务。 */
  public constructor(private readonly sectors: SectorMarketDataService) {}

  @Post(':exchange/:symbol/sectors')
  @HttpCode(HttpStatus.OK)
  /** 返回一只证券在指定 scheme 固定 release 下的板块观察归属。 */
  public async list(
    @Param() path: EquityMembershipPathDto,
    @Query() query: ListEquitySectorsQueryDto,
    @Headers('if-none-match') ifNoneMatch: string | undefined,
    @Res({ passthrough: true }) response: Response,
  ): Promise<unknown> {
    const result = await this.sectors.listEquitySectors(path, query, ifNoneMatch);
    if (result.etag !== undefined) response.setHeader('ETag', result.etag);
    response.setHeader('Cache-Control', 'private, max-age=0, must-revalidate');
    if (result.status === 304) {
      // 公开 POST 不能返回仅适用于条件 GET/HEAD 的 304，因此保留无体语义并映射为 204。
      response.status(204).send();
      return undefined;
    }
    response.setHeader('X-Data-Version', result.body.release.dataVersion);
    return result.body;
  }
}
