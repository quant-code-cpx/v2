import 'reflect-metadata';

import { plainToInstance } from 'class-transformer';
import { validate } from 'class-validator';
import { describe, expect, it } from 'vitest';

import { EquityTemporalQueryDto } from '../dto/equity-temporal-query.dto.js';
import { ListEquitiesQueryDto } from '../dto/list-equities-query.dto.js';
import { ListListingStatusHistoryQueryDto } from '../dto/list-listing-status-history-query.dto.js';

/** 覆盖 0010 查询合同的日期、时刻、重复参数与页大小转换。 */
describe('Equity instrument DTO validation', () => {
  /** 验证 `date` 参数不能接受语法有效但口径错误的完整时间戳。 */
  it('rejects a timestamp where a date-only value is required', async () => {
    const input = plainToInstance(EquityTemporalQueryDto, {
      asOf: '2026-07-01T00:00:00Z',
    });

    await expect(validate(input)).resolves.not.toHaveLength(0);
  });

  /** 验证知识时刻必须包含时间和明确偏移量，避免跨时区歧义。 */
  it('rejects a date-only or offset-free knowledge instant', async () => {
    const dateOnly = plainToInstance(EquityTemporalQueryDto, {
      knownAt: '2026-07-01',
    });
    const offsetFree = plainToInstance(EquityTemporalQueryDto, {
      knownAt: '2026-07-01T08:00:00',
    });

    const [dateOnlyErrors, offsetFreeErrors] = await Promise.all([
      validate(dateOnly),
      validate(offsetFree),
    ]);
    expect(dateOnlyErrors).not.toHaveLength(0);
    expect(offsetFreeErrors).not.toHaveLength(0);
  });

  /** 验证重复状态、查询前缀和数字页大小按公开合同完成转换。 */
  it('normalizes a valid list query before service execution', async () => {
    const input = plainToInstance(ListEquitiesQueryDto, {
      status: ['LISTED', 'SUSPENDED'],
      query: ' 浦发 ',
      limit: '100',
      asOf: '2026-07-01',
      knownAt: '2026-07-01T08:00:00+08:00',
    });

    await expect(validate(input)).resolves.toHaveLength(0);
    expect(input).toMatchObject({
      status: ['LISTED', 'SUSPENDED'],
      query: '浦发',
      limit: 100,
    });
  });

  /** 验证历史区间各边界同样只接受纯日期。 */
  it('rejects a timestamp in a listing history date bound', async () => {
    const input = plainToInstance(ListListingStatusHistoryQueryDto, {
      effectiveFrom: '2026-07-01T00:00:00Z',
      limit: '50',
    });

    await expect(validate(input)).resolves.not.toHaveLength(0);
  });
});
