import { Injectable } from '@nestjs/common';

import { MarketDataAccessClient } from '../../data-sync/clients/market-data-access.client.js';
import type { MarketDataQueryResponse } from '../../data-sync/contracts/market-data-access.contract.js';

/** 编排公开市场数据查询，不复制 data-sync 的选择或字段治理规则。 */
@Injectable()
export class MarketDataAccessService {
  /** 接收唯一允许访问同步服务的防腐客户端。 */
  public constructor(private readonly client: MarketDataAccessClient) {}

  /** 将用户的只读查询经安全客户端转交给 data-sync，并原样保留空 records。 */
  public query(request: unknown, requestId: string): Promise<MarketDataQueryResponse> {
    return this.client.query({ request, requestId });
  }
}
