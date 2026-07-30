import 'reflect-metadata';

import { validate } from 'class-validator';
import { describe, expect, it } from 'vitest';

import {
  ListMarketIndexBarsBodyDto,
  ListSwIndustryBarsBodyDto,
  MarketIndexPathDto,
} from '../dto/market-overview.dto.js';

describe('market overview DTOs', () => {
  /** 验证四个稳定主要指数 ID 都能穿过公开路径校验。 */
  it('accepts every published primary index identity', async () => {
    const indexIds = ['sse-composite', 'szse-component', 'csi-300', 'chinext'] as const;

    for (const indexId of indexIds) {
      const input = Object.assign(new MarketIndexPathDto(), { indexId });
      await expect(validate(input)).resolves.toHaveLength(0);
    }
  });

  /** 验证供应商代码或任意自由文本不能绕过稳定指数身份映射。 */
  it('rejects identities outside the frozen primary index set', async () => {
    const input = Object.assign(new MarketIndexPathDto(), { indexId: '000001.SH' });

    await expect(validate(input)).resolves.not.toHaveLength(0);
  });

  /** 验证指数查询只接受日线，避免 API 请求线程生成未发布周期。 */
  it('keeps primary index bars on source daily publications', async () => {
    const input = Object.assign(new ListMarketIndexBarsBodyDto(), {
      period: '1w',
      start: '2026-01-01',
      end: '2026-07-30',
    });

    await expect(validate(input)).resolves.not.toHaveLength(0);
  });

  /** 验证申万详情可选择同步阶段已物化的日、周、月 publication。 */
  it('accepts all materialized SW industry bar periods', async () => {
    const periods = ['1d', '1w', '1mo'] as const;

    for (const period of periods) {
      const input = Object.assign(new ListSwIndustryBarsBodyDto(), {
        period,
        start: '2026-01-01',
        end: '2026-07-30',
      });
      await expect(validate(input)).resolves.toHaveLength(0);
    }
  });
});
