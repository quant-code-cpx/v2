import { z } from 'zod';

/** 约束公开与内部证券主数据共同支持的交易所。 */
export const EQUITY_EXCHANGES = ['SSE', 'SZSE', 'BSE'] as const;

/** 约束已发布的上市生命周期状态，不包含内部 `PENDING`。 */
export const EQUITY_LISTING_STATUSES = ['LISTED', 'SUSPENDED', 'DELISTED'] as const;

/** 约束来源日期的精度声明，防止观测日期伪装成官方日期。 */
export const EFFECTIVE_DATE_PRECISIONS = ['OFFICIAL_DATE', 'OBSERVATION_DATE'] as const;

const exchangeSchema = z.enum(EQUITY_EXCHANGES);
const symbolSchema = z.string().regex(/^[0-9]{6}$/);
const listingStatusSchema = z.enum(EQUITY_LISTING_STATUSES);
const effectiveDatePrecisionSchema = z.enum(EFFECTIVE_DATE_PRECISIONS);
const dateSchema = z.string().date();
const dateTimeSchema = z.string().datetime({ offset: true });
const nullableDateSchema = dateSchema.nullable();
const nullableDateTimeSchema = dateTimeSchema.nullable();

/** 校验不含 raw URI 的来源批次锚点，供两个服务核对数据血缘。 */
const sourceAttributionSchema = z
  .object({
    sourceBatchId: z.string().uuid(),
    providerId: z.string().min(1).max(100),
    upstreamSource: z.string().min(1).max(100),
  })
  .strict();

/** 约束已发布输入组件的质量状态；resolved 主数据只接受 passed 输入。 */
const publicationQualityStatusSchema = z.literal('passed');

/** 校验目录和交易所生命周期可以公开审计的证据类型。 */
const lifecycleEvidenceKindSchema = z.enum([
  'CATALOG',
  'EXPLICIT_LISTING',
  'EXPLICIT_SUSPENSION',
  'EXPLICIT_RESUMPTION',
  'EXPLICIT_DELISTING',
  'OFFICIAL_CORRECTION',
]);

/** 校验 resolved publication 固定采用的一个目录或生命周期输入组件。 */
const publicationComponentSchema = z
  .object({
    componentKey: z.string().min(1).max(64),
    dataset: z.enum(['equity.master.catalog', 'equity.lifecycle.explicit']),
    partitionKey: exchangeSchema,
    dataVersion: z.string().uuid(),
    publishedAt: dateTimeSchema,
    effectiveAsOf: dateSchema,
    knowledgeCutoff: dateTimeSchema,
    qualityStatus: publicationQualityStatusSchema,
  })
  .strict();

/** 校验一个证券代码在业务有效时间与系统知识时间中的身份。 */
const temporalIdentifierSchema = z
  .object({
    exchange: exchangeSchema,
    symbol: symbolSchema,
    effectiveFrom: dateSchema,
    effectiveTo: nullableDateSchema,
    datePrecision: effectiveDatePrecisionSchema,
    knownFrom: dateTimeSchema,
    observedAt: dateTimeSchema,
    source: sourceAttributionSchema,
    qualityStatus: publicationQualityStatusSchema,
  })
  .strict();

/** 校验证券名称的双时态投影。 */
const temporalNameSchema = z
  .object({
    value: z.string().min(1).max(200),
    effectiveFrom: dateSchema,
    effectiveTo: nullableDateSchema,
    datePrecision: effectiveDatePrecisionSchema,
    knownFrom: dateTimeSchema,
    observedAt: dateTimeSchema,
    source: sourceAttributionSchema,
    qualityStatus: publicationQualityStatusSchema,
  })
  .strict();

/** 校验上市生命周期的双时态投影。 */
const temporalListingSchema = z
  .object({
    status: listingStatusSchema,
    listedOn: nullableDateSchema,
    delistedOn: nullableDateSchema,
    effectiveFrom: dateSchema,
    effectiveTo: nullableDateSchema,
    datePrecision: effectiveDatePrecisionSchema,
    knownFrom: dateTimeSchema,
    observedAt: dateTimeSchema,
    evidenceKind: lifecycleEvidenceKindSchema,
    source: sourceAttributionSchema,
    qualityStatus: publicationQualityStatusSchema,
  })
  .strict();

/** 校验同步服务返回的证券条目，包含仅供服务间关联的内部 UUID。 */
const internalEquityItemSchema = z
  .object({
    instrumentId: z.string().uuid(),
    identifier: temporalIdentifierSchema,
    name: temporalNameSchema,
    listing: temporalListingSchema,
  })
  .strict();

/** 校验公开证券条目，确保内部 UUID 已被裁剪。 */
export const equityItemSchema = internalEquityItemSchema.omit({ instrumentId: true }).strict();

/** 校验同步服务返回的稳定目录发布页。 */
export const internalEquityPageSchema = z
  .object({
    items: z.array(internalEquityItemSchema).max(200),
    nextCursor: z.string().max(1024).nullable(),
    dataVersion: z.string().uuid(),
    publishedAt: dateTimeSchema,
    effectiveAsOf: dateSchema,
    requestedKnownAt: dateTimeSchema,
    publicationScope: z.enum(['SSE', 'SZSE', 'BSE', 'CN_A_STABLE']),
    componentPublications: z.array(publicationComponentSchema).min(2).max(6),
  })
  .strict();

/** 校验公开目录页，限制公开页最多返回一百条。 */
export const equityPageSchema = z
  .object({
    items: z.array(equityItemSchema).max(100),
    nextCursor: z.string().max(1024).nullable(),
    dataVersion: z.string().uuid(),
    publishedAt: dateTimeSchema,
    effectiveAsOf: dateSchema,
    requestedKnownAt: dateTimeSchema,
    publicationScope: z.enum(['SSE', 'SZSE', 'BSE', 'CN_A_STABLE']),
    componentPublications: z.array(publicationComponentSchema).min(2).max(6),
  })
  .strict();

/** 校验同步服务返回的单证券详情。 */
export const internalEquityDetailSchema = z
  .object({
    instrumentId: z.string().uuid(),
    identifier: temporalIdentifierSchema,
    name: temporalNameSchema,
    listing: temporalListingSchema,
    dataVersion: z.string().uuid(),
    publishedAt: dateTimeSchema,
    effectiveAsOf: dateSchema,
    requestedKnownAt: dateTimeSchema,
    publicationScope: z.enum(['SSE', 'SZSE', 'BSE', 'CN_A_STABLE']),
    componentPublications: z.array(publicationComponentSchema).min(2).max(6),
  })
  .strict();

/** 校验公开单证券详情，确保没有服务内关联 UUID。 */
export const equityDetailSchema = internalEquityDetailSchema.omit({ instrumentId: true }).strict();

/** 校验一段上市状态的业务与知识有效区间。 */
const listingStatusPeriodSchema = z
  .object({
    status: listingStatusSchema,
    effectiveFrom: dateSchema,
    effectiveTo: nullableDateSchema,
    effectiveDatePrecision: effectiveDatePrecisionSchema,
    knownFrom: dateTimeSchema,
    knownTo: nullableDateTimeSchema,
    observedAt: dateTimeSchema,
    evidenceKind: lifecycleEvidenceKindSchema,
    source: sourceAttributionSchema,
    qualityStatus: publicationQualityStatusSchema,
  })
  .strict();

/** 校验同步服务返回的上市状态历史页。 */
export const internalListingStatusHistoryPageSchema = z
  .object({
    instrumentId: z.string().uuid(),
    exchange: exchangeSchema,
    symbol: symbolSchema,
    items: z.array(listingStatusPeriodSchema).max(200),
    nextCursor: z.string().max(1024).nullable(),
    dataVersion: z.string().uuid(),
    publishedAt: dateTimeSchema,
    requestedKnownAt: dateTimeSchema,
    publicationScope: z.enum(['SSE', 'SZSE', 'BSE', 'CN_A_STABLE']),
    componentPublications: z.array(publicationComponentSchema).min(2).max(6),
  })
  .strict();

/** 校验公开上市状态历史页，限制公开页最多返回一百条。 */
export const listingStatusHistoryPageSchema = internalListingStatusHistoryPageSchema
  .omit({ instrumentId: true })
  .extend({ items: z.array(listingStatusPeriodSchema).max(100) })
  .strict();

/** 描述同步服务内部目录页。 */
export type InternalEquityPage = z.infer<typeof internalEquityPageSchema>;

/** 描述公开证券目录页。 */
export type EquityPage = z.infer<typeof equityPageSchema>;

/** 描述同步服务内部证券详情。 */
export type InternalEquityDetail = z.infer<typeof internalEquityDetailSchema>;

/** 描述公开证券详情。 */
export type EquityDetail = z.infer<typeof equityDetailSchema>;

/** 描述同步服务内部上市状态历史页。 */
export type InternalListingStatusHistoryPage = z.infer<
  typeof internalListingStatusHistoryPageSchema
>;

/** 描述公开上市状态历史页。 */
export type ListingStatusHistoryPage = z.infer<typeof listingStatusHistoryPageSchema>;
