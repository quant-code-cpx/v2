import { Injectable } from '@nestjs/common';

import type { GetSwIndustryQueryDto } from './dto/get-sw-industry-query.dto.js';
import type { ListSwIndustriesQueryDto } from './dto/list-sw-industries-query.dto.js';
import type { ListSwValuationsQueryDto } from './dto/list-sw-valuations-query.dto.js';
import type { SwIndustryPathDto } from './dto/sw-industry-path.dto.js';
import {
  SwSectorClient,
  type SwUpstreamResponse,
} from '../../data-sync/clients/sw-sector.client.js';
import type {
  SwIndustryPage,
  SwIndustryResource,
  SwIndustryValuationPage,
} from '../../data-sync/contracts/sw-sector.contract.js';

/** 编排已认证用户的申万 taxonomy、闭包和估值只读请求。 */
@Injectable()
export class SwIndustryService {
  /** 注入唯一允许访问同步服务的申万防腐 client。 */
  public constructor(private readonly client: SwSectorClient) {}

  /** 返回指定发布和筛选范围的 taxonomy 页。 */
  public listIndustries(
    query: ListSwIndustriesQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<SwUpstreamResponse<SwIndustryPage>> {
    return this.client.listIndustries({ ...query, ifNoneMatch, requestId });
  }

  /** 返回一个行业节点及冻结发布中的父级闭包。 */
  public getIndustry(
    path: SwIndustryPathDto,
    query: GetSwIndustryQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<SwUpstreamResponse<SwIndustryResource>> {
    return this.client.getIndustry({
      code: path.code,
      snapshotDate: query.snapshotDate,
      ifNoneMatch,
      requestId,
    });
  }

  /** 返回一个发布日期的行业估值观察页。 */
  public listValuations(
    query: ListSwValuationsQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<SwUpstreamResponse<SwIndustryValuationPage>> {
    return this.client.listValuations({ ...query, ifNoneMatch, requestId });
  }
}
