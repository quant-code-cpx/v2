import { z } from 'zod';

/** 约束两类不可混合的资金流语义族。 */
export const MONEY_FLOW_SEMANTIC_FAMILIES = ['trade_direction_flow', 'order_size_flow'] as const;

/** 约束三类 canonical 观察范围。 */
export const MONEY_FLOW_SCOPE_TYPES = ['equity', 'sector', 'market'] as const;

/** 约束供应商排行支持的范围。 */
export const MONEY_FLOW_RANKING_SCOPE_TYPES = ['equity', 'sector'] as const;

/** 约束供应商快照窗口，不把逐日序列混入排行。 */
export const MONEY_FLOW_RANKING_WINDOWS = ['supplier_day', 'supplier_rolling'] as const;

/** 约束公开可查询的方法学状态。 */
export const MONEY_FLOW_PUBLIC_STATUSES = ['validated', 'retired'] as const;

const uuidSchema = z.string().uuid();
const dateSchema = z.string().date();
const dateTimeSchema = z.string().datetime({ offset: true });
const decimalSchema = z
  .string()
  .regex(/^-?[0-9]+(?:\.[0-9]+)?$/)
  .nullable();
const methodologyIdSchema = z.string().regex(/^[a-z][a-z0-9_.-]{2,99}$/);
const methodologyVersionSchema = z.string().min(1).max(64);
const semanticFamilySchema = z.enum(MONEY_FLOW_SEMANTIC_FAMILIES);
const scopeTypeSchema = z.enum(MONEY_FLOW_SCOPE_TYPES);
const rankingScopeTypeSchema = z.enum(MONEY_FLOW_RANKING_SCOPE_TYPES);
const rankingWindowSchema = z.enum(MONEY_FLOW_RANKING_WINDOWS);
const supportedMeasureSchema = z.enum(['gross_inflow', 'gross_outflow', 'net_amount', 'net_ratio']);
const finalitySchema = z.enum(['source_reported_daily', 'post_close_observation', 'unknown']);
const qualitySchema = z.enum(['passed', 'warned']);
const currencySchema = z
  .string()
  .regex(/^[A-Z]{3}$/)
  .nullable();
const bucketCodeSchema = z.string().regex(/^[a-z][a-z0-9_-]{0,63}$/);
const nullableDateTimeSchema = dateTimeSchema.nullable();

const windowDefinitionSchema = z
  .object({
    windowType: z.enum(['daily_source', ...MONEY_FLOW_RANKING_WINDOWS]),
    windowSize: z.number().int().min(1).max(252),
    label: z.string().min(1).max(100),
  })
  .strict();

const internalBucketSchema = z
  .object({
    bucket: bucketCodeSchema,
    label: z.string().min(1).max(100),
    definitionStatus: z.enum(['documented', 'inferred_unapproved', 'unknown']),
    thresholdMin: decimalSchema,
    thresholdMax: decimalSchema,
    thresholdUnit: z.string().max(32).nullable(),
  })
  .strict();

const publicBucketSchema = internalBucketSchema
  .omit({ definitionStatus: true })
  .extend({ definitionStatus: z.enum(['documented', 'unknown']) })
  .strict();

const internalMethodologySchema = z
  .object({
    methodologyUuid: uuidSchema,
    methodologyId: methodologyIdSchema,
    methodologyVersion: methodologyVersionSchema,
    methodologyStatus: z.enum(['unknown', 'research', 'validated', 'retired']),
    productionEnabled: z.boolean(),
    adapterProvider: z.string().min(1).max(100),
    upstreamSource: z.string().min(1).max(100),
    sourceDataset: z.string().min(1).max(160),
    semanticFamily: semanticFamilySchema,
    scopeTypes: z.array(scopeTypeSchema).min(1),
    universeIds: z.array(z.string().min(1).max(100)).min(1),
    supportedWindows: z.array(windowDefinitionSchema).min(1),
    buckets: z.array(internalBucketSchema).min(1),
    supportedMeasures: z.array(supportedMeasureSchema).min(1),
    ratioDenominator: z.string().min(1).max(300),
    directionDefinition: z.string().min(1).max(500),
    finality: finalitySchema,
    currency: currencySchema,
    rawAmountUnit: z.string().min(1).max(64),
    standardAmountUnit: z.string().max(64).nullable(),
    conversionVersion: z.string().max(64).nullable(),
    effectiveFrom: dateTimeSchema,
    retiredAt: nullableDateTimeSchema,
  })
  .strict();

/** 校验内部方法学目录，额外身份和 adapter 字段只能停留在防腐边界内。 */
export const internalMoneyFlowMethodologyPageSchema = z
  .object({
    dataVersion: uuidSchema,
    publishedAt: dateTimeSchema,
    items: z.array(internalMethodologySchema).max(100),
    nextCursor: z.string().max(2048).nullable(),
  })
  .strict();

/** 校验公开方法学定义。 */
export const moneyFlowMethodologySchema = internalMethodologySchema
  .omit({
    methodologyUuid: true,
    productionEnabled: true,
    adapterProvider: true,
    methodologyStatus: true,
    buckets: true,
  })
  .extend({
    methodologyStatus: z.enum(MONEY_FLOW_PUBLIC_STATUSES),
    buckets: z.array(publicBucketSchema).min(1),
  })
  .strict();

/** 校验公开方法学目录页。 */
export const moneyFlowMethodologyPageSchema = z
  .object({
    dataVersion: uuidSchema,
    publishedAt: dateTimeSchema,
    items: z.array(moneyFlowMethodologySchema).max(100),
    nextCursor: z.string().max(2048).nullable(),
  })
  .strict();

const internalEquityScopeSchema = z
  .object({
    scopeType: z.literal('equity'),
    securityId: z.number().int().positive(),
    instrumentId: uuidSchema,
    exchange: z.enum(['SSE', 'SZSE', 'BSE']),
    symbol: z.string().regex(/^[0-9]{6}$/),
    name: z.string().max(200).nullable(),
  })
  .strict();

const internalSectorScopeSchema = z
  .object({
    scopeType: z.literal('sector'),
    sectorId: uuidSchema,
    scheme: z.string().min(1).max(64),
    sectorCode: z.string().min(1).max(64),
    name: z.string().max(200).nullable(),
  })
  .strict();

const marketScopeSchema = z
  .object({
    scopeType: z.literal('market'),
    marketCode: z.string().min(1).max(32),
    name: z.string().min(1).max(100),
  })
  .strict();

const equityScopeSchema = internalEquityScopeSchema
  .omit({ securityId: true, instrumentId: true })
  .strict();
const sectorScopeSchema = internalSectorScopeSchema.omit({ sectorId: true }).strict();
const internalScopeSchema = z.discriminatedUnion('scopeType', [
  internalEquityScopeSchema,
  internalSectorScopeSchema,
  marketScopeSchema,
]);
const publicScopeSchema = z.discriminatedUnion('scopeType', [
  equityScopeSchema,
  sectorScopeSchema,
  marketScopeSchema,
]);

const dailyObservationSchema = z
  .object({
    tradeDate: dateSchema,
    observedAt: dateTimeSchema,
    knownFrom: dateTimeSchema,
    finality: finalitySchema,
    grossInflow: decimalSchema,
    grossOutflow: decimalSchema,
    netAmount: decimalSchema,
    netRatio: decimalSchema,
    qualityStatus: qualitySchema,
  })
  .strict();

const methodologyValueFields = {
  methodologyId: methodologyIdSchema,
  methodologyVersion: methodologyVersionSchema,
  upstreamSource: z.string().min(1).max(100),
  sourceDataset: z.string().min(1).max(160),
  semanticFamily: semanticFamilySchema,
  supportedMeasures: z.array(supportedMeasureSchema).min(1),
  ratioDenominator: z.string().min(1).max(300),
  directionDefinition: z.string().min(1).max(500),
  currency: currencySchema,
  amountUnit: z.string().min(1).max(64),
};

/** 校验内部日序列页，包含仅供服务间关联的 series 和 canonical 主键。 */
export const internalMoneyFlowDailyPageSchema = z
  .object({
    seriesId: uuidSchema,
    ...methodologyValueFields,
    scope: internalScopeSchema,
    universe: z.string().min(1).max(100),
    bucket: bucketCodeSchema,
    windowType: z.literal('daily_source'),
    windowSize: z.literal(1),
    knownAtApplied: nullableDateTimeSchema,
    dataVersion: uuidSchema,
    publishedAt: dateTimeSchema,
    items: z.array(dailyObservationSchema).max(500),
    nextCursor: z.string().max(2048).nullable(),
  })
  .strict();

/** 校验公开日序列页。 */
export const moneyFlowDailyPageSchema = z
  .object({
    ...methodologyValueFields,
    scope: publicScopeSchema,
    universe: z.string().min(1).max(100),
    bucket: bucketCodeSchema,
    windowType: z.literal('daily_source'),
    windowSize: z.literal(1),
    knownAtApplied: nullableDateTimeSchema,
    dataVersion: uuidSchema,
    publishedAt: dateTimeSchema,
    items: z.array(dailyObservationSchema).max(500),
    nextCursor: z.string().max(2048).nullable(),
  })
  .strict();

const internalRankingScopeSchema = z.discriminatedUnion('scopeType', [
  internalEquityScopeSchema,
  internalSectorScopeSchema,
]);
const publicRankingScopeSchema = z.discriminatedUnion('scopeType', [
  equityScopeSchema,
  sectorScopeSchema,
]);
const rankingMetricFields = {
  supplierPosition: z.number().int().positive(),
  grossInflow: decimalSchema,
  grossOutflow: decimalSchema,
  netAmount: decimalSchema,
  netRatio: decimalSchema,
};
const internalRankingItemSchema = z
  .object({ ...rankingMetricFields, scope: internalRankingScopeSchema })
  .strict();
const publicRankingItemSchema = z
  .object({ ...rankingMetricFields, scope: publicRankingScopeSchema })
  .strict();
const rankingFields = {
  ...methodologyValueFields,
  scopeType: rankingScopeTypeSchema,
  universe: z.string().min(1).max(100),
  targetTradeDate: dateSchema,
  sourceCutoffAt: dateTimeSchema,
  observedAt: dateTimeSchema,
  finality: finalitySchema,
  windowType: rankingWindowSchema,
  windowSize: z.number().int().min(1).max(252),
  bucket: bucketCodeSchema,
  rankingBasis: z.enum(['supplier_reported_order', 'supplier_order_unknown']),
  qualityStatus: qualitySchema,
  dataVersion: uuidSchema,
  publishedAt: dateTimeSchema,
  nextCursor: z.string().max(2048).nullable(),
};

/** 校验内部不可变排行页及内部 scope 身份。 */
export const internalMoneyFlowRankingPageSchema = z
  .object({
    snapshotId: uuidSchema,
    ...rankingFields,
    items: z.array(internalRankingItemSchema).max(500),
  })
  .strict();

/** 校验公开不可变排行页。 */
export const moneyFlowRankingPageSchema = z
  .object({
    ...rankingFields,
    items: z.array(publicRankingItemSchema).max(500),
  })
  .strict();

export type InternalMoneyFlowMethodologyPage = z.infer<
  typeof internalMoneyFlowMethodologyPageSchema
>;
export type InternalMoneyFlowDailyPage = z.infer<typeof internalMoneyFlowDailyPageSchema>;
export type InternalMoneyFlowRankingPage = z.infer<typeof internalMoneyFlowRankingPageSchema>;
export type MoneyFlowMethodologyPage = z.infer<typeof moneyFlowMethodologyPageSchema>;
export type MoneyFlowDailyPage = z.infer<typeof moneyFlowDailyPageSchema>;
export type MoneyFlowRankingPage = z.infer<typeof moneyFlowRankingPageSchema>;
