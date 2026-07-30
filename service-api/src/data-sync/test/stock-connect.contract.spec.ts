import { describe, expect, it } from 'vitest';

import {
  stockConnectActiveSecurityPageSchema,
  stockConnectOverviewResponseSchema,
  stockConnectReadinessResponseSchema,
  stockConnectSecurityContextResponseSchema,
} from '../contracts/stock-connect.contract.js';
import {
  derivedMoney,
  reportedMoney,
  stockConnectActiveSecurityPage,
  stockConnectOverviewResponse,
  stockConnectReadinessResponse,
  stockConnectSecurityContextResponse,
} from './stock-connect.test-data.js';

/** 覆盖最终 0024 字段判别、版本一致性、排名与真实性约束。 */
describe('stock-connect internal contract', () => {
  /** 验证完整总览、活跃证券和证券上下文样本符合严格合同。 */
  it('accepts complete final contract responses', () => {
    expect(
      stockConnectOverviewResponseSchema.safeParse(stockConnectOverviewResponse()).success,
    ).toBe(true);
    expect(
      stockConnectActiveSecurityPageSchema.safeParse(stockConnectActiveSecurityPage()).success,
    ).toBe(true);
    expect(
      stockConnectSecurityContextResponseSchema.safeParse(stockConnectSecurityContextResponse())
        .success,
    ).toBe(true);
    expect(
      stockConnectReadinessResponseSchema.safeParse(stockConnectReadinessResponse()).success,
    ).toBe(true);
  });

  /** 验证北向会话状态由交易日历和日终性联合判定时可携带同步侧真实质量告警。 */
  it('accepts the calendar-derived session-state quality issue', () => {
    const response = stockConnectOverviewResponse();
    const publication = record(response.publication);
    publication.qualityStatus = 'APPROVED_WITH_WARNINGS';
    publication.qualityIssues = [
      {
        code: 'SESSION_STATE_DERIVED_FROM_CALENDAR_AND_FINALITY',
        component: 'SH_NORTHBOUND.status.sessionState',
        detail: '来源没有独立会话字段，按交易日历与日终文件联合判定。',
      },
    ];

    expect(stockConnectOverviewResponseSchema.safeParse(response).success).toBe(true);
  });

  /** 验证来源接收时间不能冒充来源 publication 时间。 */
  it('binds source publication availability to its nullable timestamp', () => {
    const response = stockConnectOverviewResponse();
    const publication = record(response.publication);
    const source = record(records(publication.sourceRefs)[0]);
    source.sourcePublicationAt = '2026-07-29T10:05:00Z';

    expect(stockConnectOverviewResponseSchema.safeParse(response).success).toBe(false);

    source.sourcePublicationAvailability = 'REPORTED';
    source.sourcePublicationAt = null;
    expect(stockConnectOverviewResponseSchema.safeParse(response).success).toBe(false);
  });

  /** 验证 CountFact lineage、趋势版本和当前 publication 版本都不可缺失或冲突。 */
  it('rejects missing lineage and trend version drift', () => {
    const missingLineage = stockConnectOverviewResponse();
    const channel = record(records(missingLineage.channels)[0]);
    const stats = record(channel.stats);
    delete record(stats.tradeCount).lineageRef;
    expect(stockConnectOverviewResponseSchema.safeParse(missingLineage).success).toBe(false);

    const missingVersion = stockConnectOverviewResponse();
    delete record(records(missingVersion.trend)[0]).dataVersion;
    expect(stockConnectOverviewResponseSchema.safeParse(missingVersion).success).toBe(false);

    const mismatchedVersion = stockConnectOverviewResponse();
    record(records(mismatchedVersion.trend)[0]).dataVersion = 'another-version';
    expect(stockConnectOverviewResponseSchema.safeParse(mismatchedVersion).success).toBe(false);
  });

  /** 验证额度状态只能使用合同指定的 availability 和人民币余额。 */
  it('binds quota state to availability and CNY', () => {
    const response = stockConnectOverviewResponse();
    const channel = record(records(response.channels)[0]);
    const status = record(channel.status);
    const balance = record(status.quotaBalance);
    record(balance.value).currency = 'HKD';

    expect(stockConnectOverviewResponseSchema.safeParse(response).success).toBe(false);
  });

  /** 验证成交字段非负、净额派生且精确满足同币种 buy±sell 恒等式。 */
  it('enforces amount signs, provenance and exact money identities', () => {
    const valid = stockConnectOverviewResponse();
    const validStats = record(record(records(valid.channels)[0]).stats);
    validStats.buyAmount = reportedMoney('40.00');
    validStats.sellAmount = reportedMoney('60.00');
    validStats.turnoverAmount = reportedMoney('100.00');
    validStats.netBuyAmount = derivedMoney('-20.00');
    expect(stockConnectOverviewResponseSchema.safeParse(valid).success).toBe(true);

    const negativeTurnover = stockConnectOverviewResponse();
    record(record(records(negativeTurnover.channels)[0]).stats).turnoverAmount =
      reportedMoney('-1.00');
    expect(stockConnectOverviewResponseSchema.safeParse(negativeTurnover).success).toBe(false);

    const derivedBuy = stockConnectOverviewResponse();
    record(record(records(derivedBuy.channels)[0]).stats).buyAmount = derivedMoney('40.00');
    expect(stockConnectOverviewResponseSchema.safeParse(derivedBuy).success).toBe(false);

    const reportedNet = stockConnectOverviewResponse();
    record(record(records(reportedNet.channels)[0]).stats).netBuyAmount = reportedMoney('-20.00');
    expect(stockConnectOverviewResponseSchema.safeParse(reportedNet).success).toBe(false);

    const mismatchedIdentity = stockConnectOverviewResponse();
    const mismatchedStats = record(record(records(mismatchedIdentity.channels)[0]).stats);
    mismatchedStats.buyAmount = reportedMoney('40.00');
    mismatchedStats.sellAmount = reportedMoney('60.00');
    mismatchedStats.turnoverAmount = reportedMoney('99.99');
    mismatchedStats.netBuyAmount = derivedMoney('-19.99');
    expect(stockConnectOverviewResponseSchema.safeParse(mismatchedIdentity).success).toBe(false);

    const negativeQuota = stockConnectOverviewResponse();
    const negativeQuotaStatus = record(record(records(negativeQuota.channels)[0]).status);
    negativeQuotaStatus.quotaBalance = reportedMoney('-1.00', 'CNY');
    expect(stockConnectOverviewResponseSchema.safeParse(negativeQuota).success).toBe(false);
  });

  /** 验证来源活跃证券行采用与市场统计相同的精确金额恒等式。 */
  it('enforces exact money identities inside source-active rows', () => {
    const validPage = stockConnectActiveSecurityPage();
    const validItem = record(records(validPage.items)[0]);
    validItem.buyAmount = reportedMoney('40.00');
    validItem.sellAmount = reportedMoney('60.00');
    validItem.turnoverAmount = reportedMoney('100.00');
    validItem.netBuyAmount = derivedMoney('-20.00');
    expect(stockConnectActiveSecurityPageSchema.safeParse(validPage).success).toBe(true);

    const invalidPage = stockConnectActiveSecurityPage();
    const invalidItem = record(records(invalidPage.items)[0]);
    invalidItem.buyAmount = reportedMoney('40.00');
    invalidItem.sellAmount = reportedMoney('60.00');
    invalidItem.turnoverAmount = reportedMoney('100.01');
    invalidItem.netBuyAmount = derivedMoney('-20.00');
    expect(stockConnectActiveSecurityPageSchema.safeParse(invalidPage).success).toBe(false);
  });

  /** 验证 readiness 精确日、状态原因、bundle 版本和证据最大时间必须自洽。 */
  it('binds readiness states to persisted evidence and date semantics', () => {
    const invalidReason = stockConnectReadinessResponse();
    record(records(invalidReason.channels)[0]).reasonCode = 'EXECUTION_FAILED';
    expect(stockConnectReadinessResponseSchema.safeParse(invalidReason).success).toBe(false);

    const missingReadyBundle = stockConnectReadinessResponse();
    record(records(missingReadyBundle.channels)[0]).bundleDataVersion = null;
    expect(stockConnectReadinessResponseSchema.safeParse(missingReadyBundle).success).toBe(false);

    const staleObservedAt = stockConnectReadinessResponse();
    staleObservedAt.observedAt = '2026-07-29T09:00:00Z';
    expect(stockConnectReadinessResponseSchema.safeParse(staleObservedAt).success).toBe(false);

    const queryClockObservedAt = stockConnectReadinessResponse();
    queryClockObservedAt.observedAt = '2026-07-29T10:21:00Z';
    expect(stockConnectReadinessResponseSchema.safeParse(queryClockObservedAt).success).toBe(false);

    const exactPending = stockConnectReadinessResponse();
    exactPending.mode = 'EXACT';
    exactPending.requestedExactDate = '2026-07-29';
    exactPending.readyTradeDate = null;
    const exactChannel = record(records(exactPending.channels)[0]);
    exactChannel.state = 'PENDING';
    exactChannel.reasonCode = 'EXECUTION_PENDING';
    exactChannel.bundleDataVersion = null;
    expect(stockConnectReadinessResponseSchema.safeParse(exactPending).success).toBe(true);
  });

  /** 验证来源榜与榜内净额榜不能互相伪装，且不可用净额必须为空页。 */
  it('enforces ranking availability and empty unavailable net pages', () => {
    const sourcePage = stockConnectActiveSecurityPage();
    sourcePage.rankingAvailability = 'DERIVED';
    expect(stockConnectActiveSecurityPageSchema.safeParse(sourcePage).success).toBe(false);

    const unavailableNetPage = stockConnectActiveSecurityPage();
    unavailableNetPage.ranking = 'NET_BUY';
    unavailableNetPage.rankingAvailability = 'NOT_DISCLOSED_BY_REGIME';
    unavailableNetPage.nextCursor = 'must-not-exist';
    expect(stockConnectActiveSecurityPageSchema.safeParse(unavailableNetPage).success).toBe(false);

    const derivedNetPage = stockConnectActiveSecurityPage();
    derivedNetPage.ranking = 'NET_SELL';
    derivedNetPage.rankingAvailability = 'DERIVED';
    expect(stockConnectActiveSecurityPageSchema.safeParse(derivedNetPage).success).toBe(false);
  });

  /** 验证 rankingRank 必填、严格递增且不覆盖官方 sourceRank。 */
  it('keeps ranking rank separate and deterministic', () => {
    const missingRank = stockConnectActiveSecurityPage();
    delete record(records(missingRank.items)[0]).rankingRank;
    expect(stockConnectActiveSecurityPageSchema.safeParse(missingRank).success).toBe(false);

    const repeatedRank = stockConnectActiveSecurityPage();
    const first = record(records(repeatedRank.items)[0]);
    records(repeatedRank.items).push({ ...first, sourceRank: 2, rankingRank: 1 });
    expect(stockConnectActiveSecurityPageSchema.safeParse(repeatedRank).success).toBe(false);
  });

  /** 验证稳定身份状态与 entityRef 空值语义强绑定。 */
  it('binds identity availability to entity reference', () => {
    const page = stockConnectActiveSecurityPage();
    const item = record(records(page.items)[0]);
    const identity = record(item.identity);
    identity.identityAvailability = 'SOURCE_UNRESOLVED';

    expect(stockConnectActiveSecurityPageSchema.safeParse(page).success).toBe(false);
  });

  /** 验证券历史活动行必须携带对应 bundle dataVersion。 */
  it('requires security activity data versions', () => {
    const response = stockConnectSecurityContextResponse();
    delete record(records(response.activities)[0]).dataVersion;

    expect(stockConnectSecurityContextResponseSchema.safeParse(response).success).toBe(false);
  });
});

/** 将测试构造值收窄为可修改 JSON 对象。 */
function record(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('Expected an object');
  }
  return value as Record<string, unknown>;
}

/** 将测试构造值收窄为可修改 JSON 数组。 */
function records(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new Error('Expected an array');
  return value;
}
