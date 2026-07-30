import { BadRequestException } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { FinancialDataClient } from '../../../data-sync/clients/financial-data.client.js';
import { FinancialDataService } from '../financial-data.service.js';

const dataVersion = '00000000-0000-4000-8000-000000000001';

/** 覆盖财务公开服务的业务范围校验与防腐 client 委派。 */
describe('FinancialDataService', () => {
  /** 验证报表和指标请求完整传递方法学、双时态、游标与关联标识。 */
  it('delegates report and metric point-in-time queries', async () => {
    const client = financialClient();
    const service = new FinancialDataService(client.value);
    const path = { exchange: 'SSE', symbol: '600519' } as never;

    await service.listReports(
      path,
      {
        dataVersion,
        methodologyCode: 'eastmoney.statement',
        methodologyVersion: 1,
        statementType: ['INCOME_STATEMENT'],
        basis: ['YEAR_TO_DATE'],
        reportPeriodFrom: '2025-01-01',
        reportPeriodTo: '2025-12-31',
        limit: 20,
      } as never,
      '"reports"',
      'req-reports',
    );
    await service.listMetrics(
      path,
      {
        dataVersion,
        origin: 'PLATFORM_DERIVED',
        methodologyCode: 'platform.financial-derivation',
        methodologyVersion: 1,
        metric: ['platform.net_profit_parent.ttm'],
        basis: ['TTM'],
        limit: 200,
      } as never,
      undefined,
      'req-metrics',
    );

    expect(client.listReports).toHaveBeenCalledWith(
      expect.objectContaining({
        exchange: 'SSE',
        symbol: '600519',
        dataVersion,
        methodologyCode: 'eastmoney.statement',
        requestId: 'req-reports',
      }),
    );
    expect(client.listMetrics).toHaveBeenCalledWith(
      expect.objectContaining({
        origin: 'PLATFORM_DERIVED',
        dataVersion,
        metrics: ['platform.net_profit_parent.ttm'],
        requestId: 'req-metrics',
      }),
    );
  });

  /** 验证反向范围、超长估值窗、未来 knownAt 与超长 ETag 被公开层拒绝。 */
  it('rejects invalid temporal and conditional ranges before downstream access', () => {
    const client = financialClient();
    const service = new FinancialDataService(client.value);
    const path = { exchange: 'SSE', symbol: '600519' } as never;

    /** 发起超过十年的估值窗口。 */
    function requestOversizedValuationRange(): void {
      void service.listValuations(
        path,
        {
          dataVersion,
          methodologyCode: 'eastmoney.valuation',
          methodologyVersion: 1,
          metric: ['pe_ttm'],
          start: '2010-01-01',
          end: '2026-01-01',
          limit: 500,
        },
        undefined,
        'req-long',
      );
    }

    /** 发起未来知识时刻的报表查询。 */
    function requestFutureKnowledge(): void {
      void service.listReports(
        path,
        {
          dataVersion,
          methodologyCode: 'eastmoney.statement',
          methodologyVersion: 1,
          knownAt: '2999-01-01T00:00:00Z',
          limit: 20,
        },
        undefined,
        'req-future',
      );
    }

    /** 发起携带超长条件请求头的指标查询。 */
    function requestOversizedEtag(): void {
      void service.listMetrics(
        path,
        {
          dataVersion,
          origin: 'PROVIDER_REPORTED',
          methodologyCode: 'eastmoney.metric',
          methodologyVersion: 1,
          metric: ['provider_metric.roe'],
          limit: 200,
        } as never,
        'x'.repeat(257),
        'req-etag',
      );
    }

    expect(requestOversizedValuationRange).toThrow(BadRequestException);
    expect(requestFutureKnowledge).toThrow(BadRequestException);
    expect(requestOversizedEtag).toThrow(BadRequestException);
    expect(client.listValuations).not.toHaveBeenCalled();
  });
});

/** 构造财务服务测试所需的 client spy。 */
function financialClient(): {
  value: FinancialDataClient;
  listReports: ReturnType<typeof vi.fn>;
  listMetrics: ReturnType<typeof vi.fn>;
  listValuations: ReturnType<typeof vi.fn>;
} {
  const result = {
    listReports: vi.fn().mockResolvedValue({ status: 304 }),
    getReport: vi.fn().mockResolvedValue({ status: 304 }),
    listMetrics: vi.fn().mockResolvedValue({ status: 304 }),
    listValuations: vi.fn().mockResolvedValue({ status: 304 }),
  };
  return { value: result as unknown as FinancialDataClient, ...result };
}
