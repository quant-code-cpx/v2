import 'reflect-metadata';

import { plainToInstance } from 'class-transformer';
import { validate } from 'class-validator';
import { describe, expect, it } from 'vitest';

import {
  StockConnectActiveSecurityQueryDto,
  StockConnectOverviewQueryDto,
  StockConnectReadinessQueryDto,
} from '../dto/stock-connect-query.dto.js';

/** 覆盖公开 DTO 的日期互斥、通道去重和必填游标边界。 */
describe('stock-connect query DTOs', () => {
  /** 验证 LATEST 不能携带日期，EXACT 不能使用 null。 */
  it('rejects contradictory date selection states', async () => {
    const latestWithDate = plainToInstance(StockConnectOverviewQueryDto, {
      date: { mode: 'LATEST', exactDate: '2026-07-29' },
      channels: ['SH_NORTHBOUND'],
      trendTradingDays: 20,
    });
    const exactWithoutDate = plainToInstance(StockConnectOverviewQueryDto, {
      date: { mode: 'EXACT', exactDate: null },
      channels: ['SH_NORTHBOUND'],
      trendTradingDays: 20,
    });

    expect(await validate(latestWithDate)).not.toHaveLength(0);
    expect(await validate(exactWithoutDate)).not.toHaveLength(0);
  });

  /** 验证一致的 latest 日期和互不重复通道可以通过。 */
  it('accepts a consistent overview query', async () => {
    const query = plainToInstance(StockConnectOverviewQueryDto, {
      date: { mode: 'LATEST', exactDate: null },
      channels: ['SH_NORTHBOUND', 'SZ_NORTHBOUND'],
      trendTradingDays: 20,
    });

    expect(await validate(query)).toHaveLength(0);

    const readiness = plainToInstance(StockConnectReadinessQueryDto, {
      date: { mode: 'EXACT', exactDate: '2026-07-29' },
      channels: ['SH_NORTHBOUND', 'SZ_NORTHBOUND'],
    });
    expect(await validate(readiness)).toHaveLength(0);
  });

  /** 验证重复通道和缺失首屏 null 游标会在公开入口被拒绝。 */
  it('requires unique channels and an explicit nullable cursor', async () => {
    const duplicateChannels = plainToInstance(StockConnectOverviewQueryDto, {
      date: { mode: 'LATEST', exactDate: null },
      channels: ['SH_NORTHBOUND', 'SH_NORTHBOUND'],
      trendTradingDays: 20,
    });
    const missingCursor = plainToInstance(StockConnectActiveSecurityQueryDto, {
      date: { mode: 'LATEST', exactDate: null },
      channel: 'SH_NORTHBOUND',
      ranking: 'SOURCE_ACTIVE',
      parentPublicationDataVersion: 'stock-connect.2026-07-29.revision-1',
      limit: 20,
    });

    expect(await validate(duplicateChannels)).not.toHaveLength(0);
    expect(await validate(missingCursor)).not.toHaveLength(0);
  });

  /** 验证父版本允许非 UUID 业务版本，但拒绝可造成日志或响应头注入的控制字符。 */
  it('accepts opaque publication versions and rejects control characters', async () => {
    const validVersion = plainToInstance(StockConnectActiveSecurityQueryDto, {
      date: { mode: 'LATEST', exactDate: null },
      channel: 'SH_NORTHBOUND',
      ranking: 'SOURCE_ACTIVE',
      parentPublicationDataVersion: 'stock-connect.2026-07-29.revision-1',
      cursor: null,
      limit: 20,
    });
    const controlCharacterVersion = plainToInstance(StockConnectActiveSecurityQueryDto, {
      date: { mode: 'LATEST', exactDate: null },
      channel: 'SH_NORTHBOUND',
      ranking: 'SOURCE_ACTIVE',
      parentPublicationDataVersion: 'stock-connect\nforged',
      cursor: null,
      limit: 20,
    });

    expect(await validate(validVersion)).toHaveLength(0);
    expect(await validate(controlCharacterVersion)).not.toHaveLength(0);
  });
});
