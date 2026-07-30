import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { AppConfigService } from '../../../config/app-config.service.js';
import type { StockConnectClient } from '../../../data-sync/clients/stock-connect.client.js';
import { stockConnectOverviewResponseSchema } from '../../../data-sync/contracts/stock-connect.contract.js';
import {
  STOCK_CONNECT_TEST_DATA_VERSION,
  stockConnectOverviewResponse,
} from '../../../data-sync/test/stock-connect.test-data.js';
import { StockConnectService, createStockConnectEtag } from '../stock-connect.service.js';

/** 提供已完成真实链路验收的功能开关配置。 */
const enabledConfig = { stockConnectApiEnabled: true } as AppConfigService;

/** 覆盖 representation ETag、条件 204、开关和响应预算。 */
describe('StockConnectService', () => {
  /** 验证字段顺序不影响 ETag，但 operation、筛选或分页变化必产生新 ETag。 */
  it('builds ETags from operation, canonical request and data version', () => {
    const first = createStockConnectEtag(
      'queryStockConnectOverview',
      {
        trendTradingDays: 20,
        channels: ['SH_NORTHBOUND'],
        date: { exactDate: null, mode: 'LATEST' },
      },
      STOCK_CONNECT_TEST_DATA_VERSION,
    );
    const reordered = createStockConnectEtag(
      'queryStockConnectOverview',
      {
        date: { mode: 'LATEST', exactDate: null },
        channels: ['SH_NORTHBOUND'],
        trendTradingDays: 20,
      },
      STOCK_CONNECT_TEST_DATA_VERSION,
    );
    const otherRequest = createStockConnectEtag(
      'queryStockConnectOverview',
      {
        date: { mode: 'LATEST', exactDate: null },
        channels: ['SZ_NORTHBOUND'],
        trendTradingDays: 20,
      },
      STOCK_CONNECT_TEST_DATA_VERSION,
    );
    const otherOperation = createStockConnectEtag(
      'queryStockConnectChannel',
      {
        date: { mode: 'LATEST', exactDate: null },
        channels: ['SH_NORTHBOUND'],
        trendTradingDays: 20,
      },
      STOCK_CONNECT_TEST_DATA_VERSION,
    );

    expect(reordered).toBe(first);
    expect(otherRequest).not.toBe(first);
    expect(otherOperation).not.toBe(first);
    expect(first).toMatch(/^"[A-Za-z0-9_-]{43}"$/);
  });

  /** 验证客户端原样回传强 ETag 时返回 204，裸 dataVersion 被确定性拒绝。 */
  it('returns 204 only for an exact strong ETag match', async () => {
    const response = stockConnectOverviewResponseSchema.parse(stockConnectOverviewResponse());
    const overview = vi.fn().mockResolvedValue({
      body: response,
      dataVersion: STOCK_CONNECT_TEST_DATA_VERSION,
    });
    const service = new StockConnectService(
      { overview } as unknown as StockConnectClient,
      enabledConfig,
    );
    const query = {
      date: { mode: 'LATEST' as const, exactDate: null },
      channels: ['SH_NORTHBOUND' as const],
      trendTradingDays: 20,
    };

    const initial = await service.overview(query, undefined, 'req-service');
    expect(initial.status).toBe(200);
    const cached = await service.overview(query, initial.etag, 'req-service-cache');
    expect(cached).toEqual({
      status: 204,
      dataVersion: STOCK_CONNECT_TEST_DATA_VERSION,
      etag: initial.etag,
    });
    await expect(
      service.overview(query, STOCK_CONNECT_TEST_DATA_VERSION, 'req-bare-version'),
    ).rejects.toMatchObject({
      status: HttpStatus.BAD_REQUEST,
      response: { code: 'VALIDATION_FAILED' },
    });
  });

  /** 验证默认关闭时失败关闭，绝不访问下游或返回测试数据。 */
  it('fails closed while the feature flag is disabled', async () => {
    const overview = vi.fn();
    const service = new StockConnectService(
      { overview } as unknown as StockConnectClient,
      { stockConnectApiEnabled: false } as AppConfigService,
    );

    await expect(
      service.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        undefined,
        'req-disabled',
      ),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'UPSTREAM_UNAVAILABLE' },
    });
    expect(overview).not.toHaveBeenCalled();
  });
});
