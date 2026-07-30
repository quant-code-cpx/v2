import { describe, expect, it } from 'vitest';

import {
  scheduleFrequencySchema,
  scheduleUpsertRequestSchema,
  syncPreflightRequestSchema,
  syncSubmitRequestSchema,
  targetSelectorSchema,
} from '../contracts/data-operations.contract.js';
import { validateDataOperationsRequest } from '../../apps/data-operations/data-operations.validation.js';

/** 覆盖数据运维 selector 严格并集与公开 400/422 请求边界。 */
describe('data operations contract schemas', () => {
  /** 验证合同允许的十一类 selector 均能被严格 schema 接受。 */
  it('accepts every contract target selector kind', () => {
    const selectors = [
      { kind: 'GLOBAL' },
      { kind: 'INSTRUMENT', exchange: 'SSE', symbol: '600000' },
      { kind: 'SECTOR', scheme: 'SW', sectorCode: '801010' },
      { kind: 'SCHEME', scheme: 'SW' },
      { kind: 'EXCHANGE', exchange: 'SZSE' },
      { kind: 'CONTRACT', venue: 'CFFEX', contract: 'IF2608' },
      { kind: 'ETF', operation: 'MASTER', venue: 'SSE', etf: null },
      {
        kind: 'ETF',
        operation: 'MASTER',
        venue: null,
        scope: 'ALL_VENUES',
        etf: null,
      },
      {
        kind: 'MARGIN',
        operation: 'SECURITY',
        venue: 'SSE',
        security: { kind: 'INSTRUMENT', exchange: 'SSE', symbol: '600000' },
      },
      { kind: 'STOCK_CONNECT', operation: 'MARKET', channel: 'SH', direction: 'NORTHBOUND' },
      { kind: 'STOCK_CONNECT', operation: 'MARKET', channel: 'ALL', direction: null },
      { kind: 'TRADING_EVENT', operation: 'DRAGON_TIGER' },
      { kind: 'INDEX', administrator: 'CSI', capability: 'constituents', indexCode: '000300' },
    ];

    expect(selectors.map((selector) => targetSelectorSchema.safeParse(selector).success)).toEqual(
      new Array<boolean>(13).fill(true),
    );
  });

  /** 拒绝没有执行器支持的拆分活跃榜和明确越界的跨境持仓 operation。 */
  it('rejects unsupported stock-connect operations', () => {
    expect(
      targetSelectorSchema.safeParse({
        kind: 'STOCK_CONNECT',
        operation: 'ACTIVE_SECURITY',
        channel: 'ALL',
        direction: null,
      }).success,
    ).toBe(false);
    expect(
      targetSelectorSchema.safeParse({
        kind: 'STOCK_CONNECT',
        operation: 'HOLDING',
        channel: 'ALL',
        direction: null,
      }).success,
    ).toBe(false);
  });

  /** 验证 selector 拒绝任意 Provider 参数和不完整 ETF 范围，同时保留单只 ETF 兼容形状。 */
  it('rejects arbitrary selector fields and incomplete ETF scope', () => {
    expect(
      targetSelectorSchema.safeParse({ kind: 'GLOBAL', providerCursor: 'secret' }).success,
    ).toBe(false);
    expect(
      targetSelectorSchema.safeParse({ kind: 'ETF', operation: 'MASTER', venue: null, etf: null })
        .success,
    ).toBe(false);
    expect(
      targetSelectorSchema.safeParse({
        kind: 'ETF',
        operation: 'BARS',
        venue: 'SSE',
        etf: 'SSE.510300',
      }).success,
    ).toBe(true);
  });

  /** 验证预检只接受未绑定版本的单只或全部 ETF 草稿，不能由浏览器伪造 publication。 */
  it('accepts ETF draft scopes and rejects preflight publication versions', () => {
    expect(
      syncPreflightRequestSchema.safeParse({
        targets: [etfTarget(oneEtfSelector())],
      }).success,
    ).toBe(true);
    expect(
      syncPreflightRequestSchema.safeParse({
        targets: [etfTarget(allEtfsSelector(null))],
      }).success,
    ).toBe(true);
    expect(
      syncPreflightRequestSchema.safeParse({
        targets: [etfTarget(allEtfsSelector(profileDataVersions()))],
      }).success,
    ).toBe(false);
    expect(
      syncPreflightRequestSchema.safeParse({
        targets: [
          etfTarget({
            ...allEtfsSelector(null),
            venue: 'SSE',
          }),
        ],
      }).success,
    ).toBe(false);
  });

  /** 验证 profile 主数据可显式覆盖沪深两市，且 preflight、submit 与 schedule 共用同一形状。 */
  it('supports the explicit ALL_VENUES ETF profile scope end to end', () => {
    const selector = allEtfVenuesSelector();
    expect(
      syncPreflightRequestSchema.safeParse({
        targets: [etfProfileTarget(selector)],
      }).success,
    ).toBe(true);
    expect(
      syncSubmitRequestSchema.safeParse({
        preflightId: '00000000-0000-4000-8000-000000000001',
        requestHash: 'a'.repeat(64),
        targets: [etfProfileTarget(selector)],
        reason: '同步沪深 ETF 主数据',
      }).success,
    ).toBe(true);
    expect(
      scheduleUpsertRequestSchema.safeParse({
        scheduleId: null,
        datasetCode: 'fund.etf.profile.reported',
        mode: 'INCREMENTAL',
        selector,
        targetPolicy: { policyVersion: 1, dateResolution: 'NONE' },
        frequency: {
          kind: 'DAILY',
          timezone: 'Asia/Shanghai',
          localTime: '18:30',
          dayOfWeek: null,
          dayOfMonth: null,
          intervalMinutes: null,
          calendarCode: null,
        },
        misfirePolicy: 'RUN_ONCE',
        coalesce: true,
        enabled: true,
        expectedVersion: null,
        reason: '创建沪深 ETF 主数据同步计划',
      }).success,
    ).toBe(true);
  });

  /** 验证 ETF canonical dataset 只能执行自己的固定操作，不能把 selector 当成跨数据集开关。 */
  it('binds each ETF datasetCode to one operation', () => {
    const mismatch = {
      targets: [
        etfTarget({
          ...oneEtfSelector(),
          operation: 'NAV',
        }),
      ],
    };
    expect(syncPreflightRequestSchema.safeParse(mismatch).success).toBe(false);
    expect(() => validateDataOperationsRequest(syncPreflightRequestSchema, mismatch)).toThrow(
      expect.objectContaining({ status: 422 }),
    );
    expect(
      syncPreflightRequestSchema.safeParse({
        targets: [
          {
            ...fullTarget('fund.etf.profile.reported'),
            selector: { kind: 'GLOBAL' },
          },
        ],
      }).success,
    ).toBe(false);
  });

  /** 单只 ETF 的可选冗余 venue 必须与 qualified identity 一致，冲突在公开 API 映射为 422。 */
  it('rejects a single ETF venue that conflicts with its qualified identity', () => {
    const mismatch = {
      targets: [
        etfTarget({
          ...oneEtfSelector(),
          venue: 'SZSE',
        }),
      ],
    };

    expect(syncPreflightRequestSchema.safeParse(mismatch).success).toBe(false);
    expect(() => validateDataOperationsRequest(syncPreflightRequestSchema, mismatch)).toThrow(
      expect.objectContaining({ status: 422 }),
    );
    expect(
      syncPreflightRequestSchema.safeParse({
        targets: [
          etfTarget({
            ...oneEtfSelector(),
            venue: null,
          }),
        ],
      }).success,
    ).toBe(true);
  });

  /** 验证全量 ETF 提交必须复用预检冻结的沪深 profile publication，且键集合不可扩张。 */
  it('requires both frozen ETF profile publications on submit', () => {
    const base = {
      preflightId: '00000000-0000-4000-8000-000000000001',
      requestHash: 'a'.repeat(64),
      reason: '同步全部已发布 ETF',
    };
    expect(
      syncSubmitRequestSchema.safeParse({
        ...base,
        targets: [etfTarget(allEtfsSelector(profileDataVersions()))],
      }).success,
    ).toBe(true);
    expect(
      syncSubmitRequestSchema.safeParse({
        ...base,
        targets: [etfTarget(oneEtfSelector())],
      }).success,
    ).toBe(true);
    expect(
      syncSubmitRequestSchema.safeParse({
        ...base,
        targets: [etfTarget(allEtfsSelector(null))],
      }).success,
    ).toBe(false);
    expect(
      syncSubmitRequestSchema.safeParse({
        ...base,
        targets: [
          etfTarget({
            ...allEtfsSelector(profileDataVersions()),
            profileDataVersions: {
              ...profileDataVersions(),
              BSE: '00000000-0000-4000-8000-000000000013',
            },
          }),
        ],
      }).success,
    ).toBe(false);
  });

  /** 验证同步 target 将 selector 作为必填合同字段，并保持日期模式互斥。 */
  it('requires selector on sync targets', () => {
    expect(
      syncPreflightRequestSchema.safeParse({
        targets: [
          {
            datasetCode: 'equity.daily',
            mode: 'FULL',
            dateFrom: null,
            dateTo: null,
            observationDate: null,
          },
        ],
      }).success,
    ).toBe(false);
    expect(
      syncPreflightRequestSchema.safeParse({
        targets: [fullTarget('equity.daily')],
      }).success,
    ).toBe(true);
  });

  /** 验证同批 datasetCode 重复按合同映射为 422，而非由下游猜测顺序。 */
  it('maps duplicate datasetCode to unprocessable content', () => {
    expect(() =>
      validateDataOperationsRequest(syncSubmitRequestSchema, {
        preflightId: '00000000-0000-4000-8000-000000000001',
        requestHash: 'a'.repeat(64),
        targets: [fullTarget('equity.daily'), fullTarget('equity.daily')],
        reason: '重新补齐数据',
      }),
    ).toThrow(expect.objectContaining({ status: 422 }));
  });

  /** 验证自动计划同样冻结严格 selector，不能只传 datasetCode 和模式。 */
  it('requires selector on schedule upsert', () => {
    const base = {
      scheduleId: null,
      datasetCode: 'equity.daily',
      mode: 'INCREMENTAL',
      targetPolicy: { policyVersion: 1, dateResolution: 'NONE' },
      frequency: {
        kind: 'DAILY',
        timezone: 'Asia/Shanghai',
        localTime: '18:30',
        dayOfWeek: null,
        dayOfMonth: null,
        intervalMinutes: null,
        calendarCode: null,
      },
      misfirePolicy: 'RUN_ONCE',
      coalesce: true,
      enabled: true,
      expectedVersion: null,
      reason: '创建收盘后同步计划',
    };
    expect(scheduleUpsertRequestSchema.safeParse(base).success).toBe(false);
    expect(
      scheduleUpsertRequestSchema.safeParse({ ...base, selector: { kind: 'GLOBAL' } }).success,
    ).toBe(true);
  });

  /** 验证全量 ETF 计划只保存未冻结模板，实际 profile publication 由每次触发重新解析。 */
  it('keeps ALL_ETFS schedule selectors as unresolved templates', () => {
    const base = {
      scheduleId: null,
      datasetCode: 'fund.etf.nav.1d.reported',
      mode: 'INCREMENTAL',
      targetPolicy: { policyVersion: 1, dateResolution: 'NONE' },
      frequency: {
        kind: 'DAILY',
        timezone: 'Asia/Shanghai',
        localTime: '18:30',
        dayOfWeek: null,
        dayOfMonth: null,
        intervalMinutes: null,
        calendarCode: null,
      },
      misfirePolicy: 'RUN_ONCE',
      coalesce: true,
      enabled: true,
      expectedVersion: null,
      reason: '创建全量 ETF 净值同步计划',
    };
    expect(
      scheduleUpsertRequestSchema.safeParse({
        ...base,
        selector: allEtfsSelector(null, 'NAV'),
      }).success,
    ).toBe(true);
    expect(
      scheduleUpsertRequestSchema.safeParse({
        ...base,
        selector: allEtfsSelector(profileDataVersions(), 'NAV'),
      }).success,
    ).toBe(false);
  });

  /** 验证所有不适用的频率字段仍须显式传 null，不能把合同错误延迟到异步投递。 */
  it('requires explicit null schedule frequency fields', () => {
    const frequency = {
      kind: 'DAILY',
      timezone: 'Asia/Shanghai',
      localTime: '18:30',
      dayOfWeek: null,
      dayOfMonth: null,
      intervalMinutes: null,
      calendarCode: null,
    };
    expect(scheduleFrequencySchema.safeParse(frequency).success).toBe(true);
    expect(
      scheduleFrequencySchema.safeParse({
        kind: 'DAILY',
        timezone: 'Asia/Shanghai',
        localTime: '18:30',
        dayOfWeek: null,
        dayOfMonth: null,
        intervalMinutes: null,
      }).success,
    ).toBe(false);
  });
});

/** 构造 FULL 模式的最小严格同步 target，确保每项都有明确业务 selector。 */
function fullTarget(datasetCode: string): Record<string, unknown> {
  return {
    datasetCode,
    mode: 'FULL',
    selector: { kind: 'GLOBAL' },
    dateFrom: null,
    dateTo: null,
    observationDate: null,
  };
}

/** 构造兼容既有公开合同的单只 ETF selector；代码只是用户输入的 canonical identity。 */
function oneEtfSelector(): Record<string, unknown> {
  return {
    kind: 'ETF',
    operation: 'BARS',
    venue: 'SSE',
    etf: 'SSE.510300',
  };
}

/** 构造 profile 主数据的显式沪深全市场范围，不复用 ETF identity fan-out 版本。 */
function allEtfVenuesSelector(): Record<string, unknown> {
  return {
    kind: 'ETF',
    operation: 'MASTER',
    venue: null,
    scope: 'ALL_VENUES',
    etf: null,
  };
}

/** 构造全部已发布 ETF selector，并显式传入预检前或预检后的版本状态。 */
function allEtfsSelector(
  profileVersions: Record<string, string> | null,
  operation: 'BARS' | 'NAV' = 'BARS',
): Record<string, unknown> {
  return {
    kind: 'ETF',
    operation,
    venue: null,
    scope: 'ALL_ETFS',
    etf: null,
    profileDataVersions: profileVersions,
  };
}

/** 构造预检冻结的沪深 profile publication 版本。 */
function profileDataVersions(): Record<string, string> {
  return {
    SSE: '00000000-0000-4000-8000-000000000011',
    SZSE: '00000000-0000-4000-8000-000000000012',
  };
}

/** 构造 ETF 日线同步 target，避免测试通过 datasetCode 或代码前缀猜测范围。 */
function etfTarget(selector: Record<string, unknown>): Record<string, unknown> {
  return {
    datasetCode: 'fund.etf.bar.1d.reported',
    mode: 'INCREMENTAL',
    selector,
    dateFrom: null,
    dateTo: null,
    observationDate: null,
  };
}

/** 构造 profile 主数据同步 target，固定绑定唯一 profile dataset。 */
function etfProfileTarget(selector: Record<string, unknown>): Record<string, unknown> {
  return {
    datasetCode: 'fund.etf.profile.reported',
    mode: 'INCREMENTAL',
    selector,
    dateFrom: null,
    dateTo: null,
    observationDate: null,
  };
}
