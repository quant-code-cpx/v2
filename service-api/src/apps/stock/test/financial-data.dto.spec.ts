import 'reflect-metadata';

import { plainToInstance } from 'class-transformer';
import { validate, type ValidationError } from 'class-validator';
import { describe, expect, it } from 'vitest';

import { GetFinancialReportQueryDto } from '../dto/get-financial-report-query.dto.js';
import { ListFinancialMetricsQueryDto } from '../dto/list-financial-metrics-query.dto.js';
import { ListValuationsQueryDto } from '../dto/list-valuations-query.dto.js';

/** 验证财务公开查询在进入 service 前完成数组、分页和点时边界约束。 */
describe('financial query DTOs', () => {
  /** 验证重复参数被规范为数组，数字参数被转换且合法平台派生筛选可通过。 */
  it('normalizes valid derived metric filters and numeric pagination', async () => {
    const input = plainToInstance(ListFinancialMetricsQueryDto, {
      origin: 'PLATFORM_DERIVED',
      methodologyCode: 'platform.financial-derivation',
      methodologyVersion: '1',
      metric: ['platform.operating_revenue.ttm', 'platform.net_profit_parent.ttm'],
      basis: 'TTM',
      reportPeriodFrom: '2024-01-01',
      reportPeriodTo: '2025-12-31',
      limit: '50',
    });

    expect(await validate(input)).toHaveLength(0);
    expect(input.methodologyVersion).toBe(1);
    expect(input.basis).toEqual(['TTM']);
    expect(input.limit).toBe(50);
  });

  /** 验证指标数量、来源、游标长度和页大小不能绕过 DTO 上限。 */
  it('rejects invalid origin, unbounded filters, cursor and page size', async () => {
    const input = plainToInstance(ListFinancialMetricsQueryDto, {
      origin: 'MIXED',
      methodologyCode: 'platform.financial-derivation',
      methodologyVersion: '1',
      metric: Array.from({ length: 51 }, metricCode),
      cursor: 'x'.repeat(1025),
      limit: '501',
    });

    const properties = (await validate(input)).map(validationProperty);
    expect(properties).toEqual(expect.arrayContaining(['origin', 'metric', 'cursor', 'limit']));
  });

  /** 验证报表详情和估值只接受受控指标数量及严格日期。 */
  it('rejects oversized report filters and malformed valuation dates', async () => {
    const report = plainToInstance(GetFinancialReportQueryDto, {
      metric: Array.from({ length: 101 }, metricCode),
    });
    const valuation = plainToInstance(ListValuationsQueryDto, {
      methodologyCode: 'eastmoney.valuation',
      methodologyVersion: '1',
      metric: ['pe_ttm'],
      start: '2025-02-30',
      end: '2025-12-31',
    });

    expect((await validate(report)).map(validationProperty)).toContain('metric');
    expect((await validate(valuation)).map(validationProperty)).toContain('start');
  });
});

/** 为数量边界测试生成符合单项格式的唯一指标代码。 */
function metricCode(_: unknown, index: number): string {
  return `platform.metric_${index}`;
}

/** 提取 class-validator 错误字段，保持断言不依赖错误文案。 */
function validationProperty(error: ValidationError): string {
  return error.property;
}
