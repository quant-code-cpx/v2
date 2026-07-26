import { BadRequestException, Injectable } from '@nestjs/common';

import type { ListSectorBarsQueryDto } from './dto/list-sector-bars-query.dto.js';
import type { ListSectorsQueryDto } from './dto/list-sectors-query.dto.js';
import type { SectorPathDto } from './dto/sector-path.dto.js';
import { SectorMarketDataClient, type UpstreamResponse } from './sector-market-data.client.js';
import type { SectorBarPage, SectorPage } from './sector-market-data.contract.js';

/** 编排经过认证用户的板块读取，并在 API 边界维持日期与下游契约约束。 */
@Injectable()
export class SectorMarketDataService {
  /** 注入唯一允许访问同步服务的防腐 client。 */
  public constructor(private readonly client: SectorMarketDataClient) {}

  /** 返回一个分类体系的公开目录页，保留条件请求状态。 */
  public listSectors(
    query: ListSectorsQueryDto,
    ifNoneMatch: string | undefined,
  ): Promise<UpstreamResponse<SectorPage>> {
    return this.client.listSectors({
      scheme: query.scheme,
      query: query.query,
      cursor: query.cursor,
      limit: query.limit,
      ifNoneMatch,
    });
  }

  /** 返回一个板块的直接上游日、周或月行情页，拒绝颠倒的日期窗口。 */
  public listBars(
    path: SectorPathDto,
    query: ListSectorBarsQueryDto,
    ifNoneMatch: string | undefined,
  ): Promise<UpstreamResponse<SectorBarPage>> {
    if (query.start > query.end) {
      throw new BadRequestException('start must not be after end');
    }
    return this.client.listBars({
      scheme: path.scheme,
      code: path.sectorCode,
      period: query.period,
      start: query.start,
      end: query.end,
      cursor: query.cursor,
      limit: query.limit,
      ifNoneMatch,
    });
  }
}
