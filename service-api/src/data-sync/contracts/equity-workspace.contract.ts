import { z } from 'zod';

import { EQUITY_EXCHANGES, EQUITY_LISTING_STATUSES } from './equity-instrument.contract.js';

/** 约束证券发现快照支持的普通交易状态。 */
export const EQUITY_TRADING_STATUSES = [
  'TRADED',
  'TRADE_SUSPENDED',
  'NO_SESSION',
  'NOT_APPLICABLE',
  'UNKNOWN',
] as const;

/** 约束股票中心支持的分类体系。 */
export const EQUITY_MEMBERSHIP_SCHEMES = [
  'EASTMONEY_INDUSTRY',
  'EASTMONEY_CONCEPT',
  'SW2021_L1',
  'SW2021_L2',
  'SW2021_L3',
] as const;

/** 约束当前已准入 publication 可公开声明的证券发现列。 */
export const EQUITY_DISCOVERY_CAPABILITY_COLUMNS = [
  'symbol',
  'name',
  'exchange',
  'listingStatus',
  'tradingStatus',
  'tradeDate',
  'close',
  'previousClose',
  'changeAmount',
  'changePercent',
  'volumeShares',
  'amountCny',
  'turnoverRate',
  'totalShares',
  'listedTradableAShares',
  'totalMarketCapCny',
  'floatMarketCapCny',
  'peTtm',
  'pb',
  'psTtm',
  'memberships',
] as const;

/** 约束证券发现请求词汇；未准入列保留用于返回稳定 capability-unavailable。 */
export const EQUITY_DISCOVERY_COLUMNS = [
  ...EQUITY_DISCOVERY_CAPABILITY_COLUMNS,
  'moneyFlowNetAmount',
  'moneyFlowNetRatio',
] as const;

/** 约束当前已准入 publication 可公开声明的稳定排序字段。 */
export const EQUITY_DISCOVERY_CAPABILITY_SORT_FIELDS = [
  'symbol',
  'name',
  'close',
  'changePercent',
  'amountCny',
  'turnoverRate',
  'totalMarketCap',
  'floatMarketCap',
  'peTtm',
  'pb',
] as const;

/** 约束证券发现排序请求词汇；未准入字段由业务边界显式失败关闭。 */
export const EQUITY_DISCOVERY_SORT_FIELDS = [
  ...EQUITY_DISCOVERY_CAPABILITY_SORT_FIELDS,
  'moneyFlowNetAmount',
] as const;

/** 约束股票中心统一事件族。 */
export const EQUITY_EVENT_FAMILIES = [
  'CORPORATE_ACTION',
  'EARNINGS_FORECAST',
  'EARNINGS_EXPRESS',
  'DRAGON_TIGER',
  'BLOCK_TRADE',
] as const;

/** 约束详情页可请求的数据集状态族。 */
export const EQUITY_DATASET_FAMILIES = [
  'IDENTITY',
  'COMPANY_PROFILE',
  'BARS_1D',
  'BARS_1W',
  'BARS_1MO',
  'ADJUSTMENT_FACTOR',
  'CORPORATE_ACTION',
  'FINANCIAL_REPORT',
  'FINANCIAL_INDICATOR',
  'VALUATION',
  'MONEY_FLOW',
  'INDUSTRY_MEMBERSHIP',
  'CONCEPT_MEMBERSHIP',
  'SW_INDUSTRY_MEMBERSHIP',
  'EARNINGS_FORECAST',
  'EARNINGS_EXPRESS',
  'DRAGON_TIGER',
  'BLOCK_TRADE',
] as const;

/** 约束估值筛选支持的口径。 */
export const EQUITY_VALUATION_METRICS = ['PE_TTM', 'PB', 'PS_TTM'] as const;

/** 约束资金流筛选的供应商订单规模分桶。 */
export const EQUITY_MONEY_FLOW_BUCKETS = ['MAIN'] as const;

const exchangeSchema = z.enum(EQUITY_EXCHANGES);
const symbolSchema = z.string().regex(/^[0-9]{6}$/);
const dateSchema = z.string().date();
const dateTimeSchema = z.string().datetime({ offset: true });
const decimalSchema = z.string().regex(/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/);
const nonNegativeDecimalSchema = z.string().regex(/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/);
const nullableReasonSchema = z
  .string()
  .regex(/^[A-Z][A-Z0-9_]{0,79}$/)
  .nullish();
const datasetSchema = z.string().regex(/^[a-z0-9][a-z0-9._-]{0,159}$/);
const sourceLabelSchema = z.string().min(1).max(160);
const availabilitySchema = z.enum([
  'AVAILABLE',
  'UNAVAILABLE',
  'PARTIAL',
  'EMPTY',
  'SOURCE_UNAVAILABLE',
]);

/** 校验内部 discovery 数值筛选范围。 */
const internalDecimalRangeSchema = z
  .object({
    min: decimalSchema.optional(),
    max: decimalSchema.optional(),
  })
  .strict();

/** 校验内部查询采用的冻结方法学身份。 */
const internalMethodologyFilterSchema = z
  .object({
    code: z.string().min(1).max(120),
    version: z.string().min(1).max(80).optional(),
  })
  .strict();

/** 严格校验 API 发给同步服务的证券发现请求。 */
export const internalEquitySearchRequestSchema = z
  .object({
    q: z.string().min(1).max(64).optional(),
    exchanges: z.array(exchangeSchema).min(1).max(3).optional(),
    lifecycleStatuses: z.array(z.enum(EQUITY_LISTING_STATUSES)).min(1).max(3).optional(),
    tradingStatuses: z
      .array(z.enum(EQUITY_TRADING_STATUSES))
      .min(1)
      .max(EQUITY_TRADING_STATUSES.length)
      .optional(),
    memberships: z
      .array(
        z
          .object({
            scheme: z.enum(EQUITY_MEMBERSHIP_SCHEMES),
            code: z.string().min(1).max(80),
          })
          .strict(),
      )
      .min(1)
      .max(20)
      .optional(),
    valuation: z
      .object({
        metric: z.enum(EQUITY_VALUATION_METRICS),
        methodology: internalMethodologyFilterSchema,
        range: internalDecimalRangeSchema,
      })
      .strict()
      .optional(),
    moneyFlow: z
      .object({
        methodology: internalMethodologyFilterSchema,
        bucket: z.enum(EQUITY_MONEY_FLOW_BUCKETS),
        range: internalDecimalRangeSchema,
      })
      .strict()
      .optional(),
    columns: z.array(z.enum(EQUITY_DISCOVERY_COLUMNS)).min(1).max(24).optional(),
    sort: z
      .array(
        z
          .object({
            field: z.enum(EQUITY_DISCOVERY_SORT_FIELDS),
            direction: z.enum(['ASC', 'DESC']),
          })
          .strict(),
      )
      .min(1)
      .max(3)
      .optional(),
    cursor: z.string().min(1).max(1024).optional(),
    limit: z.number().int().min(1).max(100),
    dataVersion: z.string().uuid().optional(),
  })
  .strict();

/** 严格校验 API 发给同步服务的证券事件请求。 */
export const internalEquityEventRequestSchema = z
  .object({
    families: z.array(z.enum(EQUITY_EVENT_FAMILIES)).min(1).max(5).optional(),
    asOf: dateSchema.optional(),
    start: dateSchema,
    end: dateSchema,
    knownAt: dateTimeSchema.optional(),
    cursor: z.string().min(1).max(1024).optional(),
    limit: z.number().int().min(1).max(100),
  })
  .strict();

/** 严格校验 API 发给同步服务的数据状态请求。 */
export const internalEquityDataStatusRequestSchema = z
  .object({
    families: z
      .array(z.enum(EQUITY_DATASET_FAMILIES))
      .min(1)
      .max(EQUITY_DATASET_FAMILIES.length)
      .optional(),
    asOf: dateSchema.optional(),
    knownAt: dateTimeSchema.optional(),
  })
  .strict();

/** 校验同步来源与计算口径的稳定身份。 */
export const equityMethodologySchema = z
  .object({
    code: z.string().min(1).max(120),
    version: z.string().min(1).max(80),
  })
  .strict();

/** 校验搜索快照或事件聚合发布的消费者元数据。 */
export const equityWorkspaceReleaseSchema = z
  .object({
    dataset: datasetSchema,
    dataVersion: z.string().uuid(),
    publishedAt: dateTimeSchema,
    effectiveAsOf: dateSchema.nullish(),
    knowledgeCutoff: dateTimeSchema.nullish(),
    qualityStatus: z.enum(['passed', 'warning', 'failed', 'PASSED', 'WARNING', 'FAILED']),
  })
  .strict();

/** 校验公开发布元数据，并裁剪同步服务内部 dataset 路由名。 */
export const publicEquityWorkspaceReleaseSchema = equityWorkspaceReleaseSchema
  .omit({ dataset: true })
  .strict();

/** 校验 discovery BASE publication 的全量或部分组件完整度。 */
export const equityDiscoveryReleaseSchema = equityWorkspaceReleaseSchema
  .extend({ completeness: z.enum(['FULL', 'PARTIAL']) })
  .strict();

/** 校验公开 discovery release，并保留消费者必须展示的完整度。 */
export const publicEquityDiscoveryReleaseSchema = equityDiscoveryReleaseSchema
  .omit({ dataset: true })
  .strict();

/** 校验不含同步服务内部 UUID 的证券公开身份。 */
export const equityWorkspaceIdentitySchema = z
  .object({
    exchange: exchangeSchema,
    symbol: symbolSchema,
    name: z.string().min(1).max(300),
    /** 标识该发现行可唯一解析到同一证券身份的业务日期。 */
    identityAsOf: dateSchema,
  })
  .strict();

/** 校验同步服务证券生命周期与普通交易状态。 */
const internalEquityStatusesSchema = z
  .object({
    lifecycleStatus: z.enum(EQUITY_LISTING_STATUSES),
    tradingStatus: z.enum(EQUITY_TRADING_STATUSES),
    tradingStatusReason: z.string().min(1).max(160).nullish(),
    listedOn: dateSchema.nullish(),
    delistedOn: dateSchema.nullish(),
  })
  .strict();

/** 校验公开证券状态，并明确区分暂停上市和普通停牌。 */
const publicEquityStatusesSchema = z
  .object({
    listingStatus: z.enum(EQUITY_LISTING_STATUSES),
    tradingStatus: z.enum(EQUITY_TRADING_STATUSES),
    tradingStatusReason: z.string().min(1).max(160).nullish(),
    listedOn: dateSchema.nullish(),
    delistedOn: dateSchema.nullish(),
  })
  .strict();

/** 校验同一 EOD 快照中的未复权市场字段。 */
const equityDiscoveryMarketSchema = z
  .object({
    tradeDate: dateSchema.nullish(),
    close: nonNegativeDecimalSchema.nullish(),
    previousClose: nonNegativeDecimalSchema.nullish(),
    changeAmount: decimalSchema.nullish(),
    changePercent: decimalSchema.nullish(),
    volumeShares: nonNegativeDecimalSchema.nullish(),
    amountCny: nonNegativeDecimalSchema.nullish(),
    turnoverRate: nonNegativeDecimalSchema.nullish(),
    currency: z.literal('CNY'),
    nullReason: nullableReasonSchema,
  })
  .strict();

/** 校验按未复权收盘价和生效股本计算的市值字段。 */
const equityDiscoveryCapitalizationSchema = z
  .object({
    effectiveOn: dateSchema.nullish(),
    totalShares: nonNegativeDecimalSchema.nullish(),
    listedTradableAShares: nonNegativeDecimalSchema.nullish(),
    totalMarketCapCny: nonNegativeDecimalSchema.nullish(),
    floatMarketCapCny: nonNegativeDecimalSchema.nullish(),
    currency: z.literal('CNY'),
    methodology: equityMethodologySchema.nullish(),
    nullReason: nullableReasonSchema,
  })
  .strict();

/** 校验同一快照引用的历史估值观察。 */
const equityDiscoveryValuationSchema = z
  .object({
    tradeDate: dateSchema.nullish(),
    peTtm: decimalSchema.nullish(),
    pb: decimalSchema.nullish(),
    psTtm: decimalSchema.nullish(),
    sourceLabel: sourceLabelSchema.nullish(),
    methodology: equityMethodologySchema.nullish(),
    nullReason: nullableReasonSchema,
  })
  .strict();

/** 校验 Eastmoney order-size 方法学的日频主力资金流。 */
const equityDiscoveryMoneyFlowSchema = z
  .object({
    tradeDate: dateSchema.nullish(),
    netAmountCny: decimalSchema.nullish(),
    netRatio: decimalSchema.nullish(),
    sourceLabel: sourceLabelSchema.nullish(),
    methodology: equityMethodologySchema.nullish(),
    nullReason: nullableReasonSchema,
  })
  .strict();

/** 校验行业、概念或申万分类成员关系。 */
const equityDiscoveryMembershipSchema = z
  .object({
    scheme: z.enum(EQUITY_MEMBERSHIP_SCHEMES),
    code: z.string().min(1).max(80),
    name: z.string().min(1).max(200),
    level: z.number().int().min(1).max(3).nullish(),
    observedOn: dateSchema,
  })
  .strict();

/** 校验同步服务返回的一条证券发现记录。 */
const internalEquityDiscoveryRecordSchema = z
  .object({
    identity: equityWorkspaceIdentitySchema,
    statuses: internalEquityStatusesSchema,
    market: equityDiscoveryMarketSchema,
    capitalization: equityDiscoveryCapitalizationSchema,
    valuation: equityDiscoveryValuationSchema,
    moneyFlow: equityDiscoveryMoneyFlowSchema,
    memberships: z.array(equityDiscoveryMembershipSchema).max(200),
  })
  .strict();

/** 校验浏览器可消费的一条证券发现记录。 */
export const equityDiscoveryRecordSchema = z
  .object({
    identity: equityWorkspaceIdentitySchema,
    statuses: publicEquityStatusesSchema,
    market: equityDiscoveryMarketSchema,
    capitalization: equityDiscoveryCapitalizationSchema,
    valuation: equityDiscoveryValuationSchema,
    moneyFlow: equityDiscoveryMoneyFlowSchema,
    memberships: z.array(equityDiscoveryMembershipSchema).max(200),
  })
  .strict();

/** 校验 discovery 构建所冻结的组件版本。 */
const equityDiscoveryComponentSchema = z
  .object({
    family: z.string().min(1).max(120),
    dataVersion: z.string().uuid().nullish(),
    availability: availabilitySchema,
    sourceLabel: sourceLabelSchema.nullish(),
    methodology: equityMethodologySchema.nullish(),
  })
  .strict();

/** 校验服务端可投影的列、排序和分页上限。 */
const equityDiscoveryCapabilitiesSchema = z
  .object({
    sortFields: z
      .array(z.enum(EQUITY_DISCOVERY_CAPABILITY_SORT_FIELDS))
      .max(EQUITY_DISCOVERY_CAPABILITY_SORT_FIELDS.length),
    columns: z
      .array(z.enum(EQUITY_DISCOVERY_CAPABILITY_COLUMNS))
      .max(EQUITY_DISCOVERY_CAPABILITY_COLUMNS.length),
    maxLimit: z.number().int().min(1).max(100),
  })
  .strict();

/** 校验股票中心统一 cursor 页。 */
const equityWorkspacePageSchema = z
  .object({
    nextCursor: z.string().min(1).max(1024).nullable(),
    limit: z.number().int().min(1).max(100),
  })
  .strict();

/** 对搜索 availability 与发布内容执行跨字段不变量检查。 */
function validateSearchEnvelope(
  value: {
    availability: 'AVAILABLE' | 'UNAVAILABLE';
    release: object | null;
    records: readonly unknown[];
  },
  context: z.RefinementCtx,
): void {
  if (value.availability === 'AVAILABLE' && value.release === null) {
    context.addIssue({ code: 'custom', path: ['release'], message: 'release is required' });
  }
  if (
    value.availability === 'UNAVAILABLE' &&
    (value.release !== null || value.records.length !== 0)
  ) {
    context.addIssue({
      code: 'custom',
      path: ['availability'],
      message: 'unavailable search must not contain release or records',
    });
  }
}

const internalEquitySearchResponseBaseSchema = z
  .object({
    availability: z.enum(['AVAILABLE', 'UNAVAILABLE']),
    reasonCode: nullableReasonSchema,
    release: equityDiscoveryReleaseSchema.nullable(),
    components: z.array(equityDiscoveryComponentSchema).max(64),
    capabilities: equityDiscoveryCapabilitiesSchema,
    records: z.array(internalEquityDiscoveryRecordSchema).max(100),
    page: equityWorkspacePageSchema,
  })
  .strict();

/** 严格校验同步服务证券发现查询响应。 */
export const internalEquitySearchResponseSchema =
  internalEquitySearchResponseBaseSchema.superRefine(validateSearchEnvelope);

/** 严格校验公开证券发现查询响应。 */
export const equitySearchResponseSchema = internalEquitySearchResponseBaseSchema
  .extend({
    release: publicEquityDiscoveryReleaseSchema.nullable(),
    records: z.array(equityDiscoveryRecordSchema).max(100),
  })
  .strict()
  .superRefine(validateSearchEnvelope);

/** 校验事件中一项具有单位和币种语义的结构化事实。 */
const equityEventFactSchema = z
  .object({
    code: z.string().min(1).max(120),
    value: decimalSchema.nullish(),
    valueLow: decimalSchema.nullish(),
    valueHigh: decimalSchema.nullish(),
    unit: z.string().min(1).max(80).nullish(),
    currency: z
      .string()
      .regex(/^[A-Z]{3}$/)
      .nullish(),
    text: z.string().min(1).max(2_000).nullish(),
  })
  .strict();

/** 校验同步服务内部事件，其中 eventId 只用于防腐层生成公开引用。 */
const internalEquityEventSchema = z
  .object({
    eventId: z.string().min(1).max(200),
    family: z.enum(EQUITY_EVENT_FAMILIES),
    kind: z.string().min(1).max(120),
    stage: z.string().min(1).max(120).nullish(),
    status: z.string().min(1).max(120).nullish(),
    occurredOn: dateSchema.nullish(),
    announcedOn: dateSchema.nullish(),
    reportPeriod: dateSchema.nullish(),
    title: z.string().min(1).max(500).nullish(),
    sourceLabel: sourceLabelSchema.nullish(),
    dataVersion: z.string().uuid(),
    facts: z.array(equityEventFactSchema).max(64),
  })
  .strict();

/** 校验公开事件，禁止浏览器依赖同步服务内部事件主键。 */
export const equityEventSchema = internalEquityEventSchema
  .omit({ eventId: true })
  .extend({ eventRef: z.string().regex(/^evt_[A-Za-z0-9_-]{43}$/) })
  .strict();

/** 对事件 availability 与发布内容执行跨字段不变量检查。 */
function validateEventEnvelope(
  value: {
    availability: 'AVAILABLE' | 'UNAVAILABLE';
    release: object | null;
    events: readonly unknown[];
  },
  context: z.RefinementCtx,
): void {
  if (value.availability === 'AVAILABLE' && value.release === null) {
    context.addIssue({ code: 'custom', path: ['release'], message: 'release is required' });
  }
  if (
    value.availability === 'UNAVAILABLE' &&
    (value.release !== null || value.events.length !== 0)
  ) {
    context.addIssue({
      code: 'custom',
      path: ['availability'],
      message: 'unavailable events must not contain release or events',
    });
  }
}

const internalEquityEventResponseBaseSchema = z
  .object({
    availability: z.enum(['AVAILABLE', 'UNAVAILABLE']),
    reasonCode: nullableReasonSchema,
    release: equityWorkspaceReleaseSchema.nullable(),
    events: z.array(internalEquityEventSchema).max(100),
    page: equityWorkspacePageSchema,
  })
  .strict();

/** 严格校验同步服务统一证券事件响应。 */
export const internalEquityEventResponseSchema =
  internalEquityEventResponseBaseSchema.superRefine(validateEventEnvelope);

/** 严格校验公开统一证券事件响应。 */
export const equityEventResponseSchema = internalEquityEventResponseBaseSchema
  .extend({
    release: publicEquityWorkspaceReleaseSchema.nullable(),
    events: z.array(equityEventSchema).max(100),
  })
  .strict()
  .superRefine(validateEventEnvelope);

/** 校验详情页一个数据集独立的可用性和陈旧状态。 */
const equityDatasetStatusSchema = z
  .object({
    family: z.string().min(1).max(120),
    dataset: datasetSchema,
    availability: availabilitySchema,
    freshness: z.enum(['FRESH', 'STALE', 'UNKNOWN']),
    dataVersion: z.string().uuid().nullish(),
    publishedAt: dateTimeSchema.nullish(),
    effectiveAsOf: dateSchema.nullish(),
    knowledgeCutoff: dateTimeSchema.nullish(),
    sourceLabel: sourceLabelSchema.nullish(),
    methodology: equityMethodologySchema.nullish(),
    reasonCode: nullableReasonSchema,
    retryable: z.boolean(),
  })
  .strict();

/** 严格校验详情页数据状态响应；该响应不包含任何事实记录。 */
export const equityDataStatusResponseSchema = z
  .object({
    identity: equityWorkspaceIdentitySchema.nullish(),
    datasets: z.array(equityDatasetStatusSchema).max(EQUITY_DATASET_FAMILIES.length),
  })
  .strict();

/** 描述同步服务证券发现响应。 */
export type InternalEquitySearchResponse = z.infer<typeof internalEquitySearchResponseSchema>;

/** 描述公开证券发现响应。 */
export type EquitySearchResponse = z.infer<typeof equitySearchResponseSchema>;

/** 描述同步服务证券事件响应。 */
export type InternalEquityEventResponse = z.infer<typeof internalEquityEventResponseSchema>;

/** 描述公开证券事件响应。 */
export type EquityEventResponse = z.infer<typeof equityEventResponseSchema>;

/** 描述公开详情数据状态响应。 */
export type EquityDataStatusResponse = z.infer<typeof equityDataStatusResponseSchema>;

/** 描述同步服务证券发现请求。 */
export type InternalEquitySearchRequest = z.infer<typeof internalEquitySearchRequestSchema>;

/** 描述同步服务证券事件请求。 */
export type InternalEquityEventRequest = z.infer<typeof internalEquityEventRequestSchema>;

/** 描述同步服务数据状态请求。 */
export type InternalEquityDataStatusRequest = z.infer<typeof internalEquityDataStatusRequestSchema>;
