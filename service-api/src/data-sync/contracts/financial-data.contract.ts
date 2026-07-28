import { z } from 'zod';

import { EQUITY_EXCHANGES } from './equity-instrument.contract.js';

/** 约束财务报表类型。 */
export const FINANCIAL_STATEMENT_TYPES = [
  'BALANCE_SHEET',
  'INCOME_STATEMENT',
  'CASH_FLOW_STATEMENT',
] as const;

/** 约束财务期间口径。 */
export const FINANCIAL_PERIOD_BASES = [
  'POINT_IN_TIME',
  'YEAR_TO_DATE',
  'SINGLE_QUARTER',
  'TTM',
] as const;

/** 约束财务报表合并范围。 */
export const FINANCIAL_STATEMENT_SCOPES = ['CONSOLIDATED', 'PARENT', 'UNKNOWN'] as const;

/** 约束供应商直报与平台派生来源，禁止同页混用。 */
export const FINANCIAL_METRIC_ORIGINS = ['PROVIDER_REPORTED', 'PLATFORM_DERIVED'] as const;

/** 约束当前公开估值字段。 */
export const FINANCIAL_VALUATION_METRICS = [
  'market_cap',
  'pe_ttm',
  'pe_static',
  'pb',
  'pcf',
] as const;

const dateSchema = z.string().date();
const dateTimeSchema = z.string().datetime({ offset: true });
const decimalSchema = z.string().regex(/^-?[0-9]+(?:\.[0-9]+)?$/);
const positiveDecimalSchema = z.string().regex(/^[0-9]+(?:\.[0-9]+)?$/);
const exchangeSchema = z.enum(EQUITY_EXCHANGES);
const symbolSchema = z.string().regex(/^[0-9]{6}$/);
const uuidSchema = z.string().uuid();
const methodologyCodeSchema = z.string().regex(/^[a-z][a-z0-9_.-]{2,79}$/);
const metricCodeSchema = z.string().regex(/^[a-z][a-z0-9_.-]{1,79}$/);
const periodBasisSchema = z.enum(FINANCIAL_PERIOD_BASES);
const statementScopeSchema = z.enum(FINANCIAL_STATEMENT_SCOPES);
const metricOriginSchema = z.enum(FINANCIAL_METRIC_ORIGINS);
const knowledgeBasisSchema = z.enum(['OFFICIAL_ANNOUNCEMENT', 'PROVIDER_UPDATE', 'OBSERVED_AT']);
const knowledgeConfidenceSchema = z.enum(['HIGH', 'MEDIUM', 'CONSERVATIVE']);
const currencyNullReasonSchema = z
  .enum(['NOT_APPLICABLE', 'UNKNOWN_SOURCE', 'MIXED_CURRENCIES'])
  .nullable();
const publicationFields = {
  dataVersion: uuidSchema,
  publishedAt: dateTimeSchema,
  effectiveAsOf: dateSchema,
  knowledgeCutoff: dateTimeSchema,
};

/** 校验币种及其空值原因严格二选一。 */
function hasConsistentCurrency(value: {
  currency: string | null;
  currencyNullReason: string | null;
}): boolean {
  return (value.currency === null) !== (value.currencyNullReason === null);
}

/** 校验财务事实数值及其空值原因严格二选一。 */
function hasConsistentFactValue(value: {
  value: string | null;
  nullReason: string | null;
}): boolean {
  return (value.value === null) !== (value.nullReason === null);
}

/** 校验平台派生值必须带公式版本，供应商直报值必须不携带平台公式版本。 */
function hasConsistentFormulaVersion(value: {
  origin: (typeof FINANCIAL_METRIC_ORIGINS)[number];
  formulaVersion: number | null;
}): boolean {
  return value.origin === 'PLATFORM_DERIVED'
    ? value.formulaVersion !== null
    : value.formulaVersion === null;
}

/** 校验内部报表头，内部血缘字段由公开投影移除。 */
const internalFinancialReportHeaderSchema = z
  .object({
    instrumentId: uuidSchema,
    reportRef: uuidSchema,
    exchange: exchangeSchema,
    symbol: symbolSchema,
    statementType: z.enum(FINANCIAL_STATEMENT_TYPES),
    reportPeriod: dateSchema,
    periodBasis: periodBasisSchema,
    statementScope: statementScopeSchema,
    currency: z
      .string()
      .regex(/^[A-Z]{3}$/)
      .nullable(),
    currencyNullReason: currencyNullReasonSchema,
    reportType: z.string().min(1).max(64),
    auditStatus: z.enum(['AUDITED', 'UNAUDITED', 'UNKNOWN']),
    announcementDate: dateSchema.nullable(),
    providerUpdateDate: dateTimeSchema.nullable(),
    effectiveFrom: dateSchema,
    effectiveTo: dateSchema.nullable(),
    knownFrom: dateTimeSchema,
    knownTo: dateTimeSchema.nullable(),
    knowledgeBasis: knowledgeBasisSchema,
    knowledgeConfidence: knowledgeConfidenceSchema,
    observedAt: dateTimeSchema,
    revision: z.number().int().positive(),
    methodologyCode: methodologyCodeSchema,
    methodologyVersion: z.number().int().positive(),
    sourceCode: z.string().min(1).max(80),
    qualityStatus: z.enum(['PASSED', 'WARNED']),
  })
  .strict()
  .refine(hasConsistentCurrency);

/** 校验公开报表头，只保留消费者需要的点时字段。 */
const financialReportHeaderSchema = z
  .object({
    reportRef: uuidSchema,
    exchange: exchangeSchema,
    symbol: symbolSchema,
    statementType: z.enum(FINANCIAL_STATEMENT_TYPES),
    reportPeriod: dateSchema,
    periodBasis: periodBasisSchema,
    statementScope: statementScopeSchema,
    currency: z
      .string()
      .regex(/^[A-Z]{3}$/)
      .nullable(),
    currencyNullReason: currencyNullReasonSchema,
    reportType: z.string().min(1).max(64),
    auditStatus: z.enum(['AUDITED', 'UNAUDITED', 'UNKNOWN']),
    announcementDate: dateSchema.nullable(),
    providerUpdateDate: dateTimeSchema.nullable(),
    availableFrom: dateSchema,
    knowledgeBasis: knowledgeBasisSchema,
    knowledgeConfidence: knowledgeConfidenceSchema,
    revision: z.number().int().positive(),
    methodologyCode: methodologyCodeSchema,
    methodologyVersion: z.number().int().positive(),
    qualityStatus: z.enum(['PASSED', 'WARNED']),
  })
  .strict()
  .refine(hasConsistentCurrency);

/** 校验内部治理报表行项目。 */
const internalFinancialStatementItemSchema = z
  .object({
    metricCode: metricCodeSchema,
    label: z.string().min(1).max(160),
    value: decimalSchema.nullable(),
    nullReason: z.enum(['NOT_REPORTED', 'NOT_APPLICABLE', 'UPSTREAM_NULL']).nullable(),
    currency: z
      .string()
      .regex(/^[A-Z]{3}$/)
      .nullable(),
    currencyNullReason: currencyNullReasonSchema,
    originalUnit: z.string().min(1).max(32),
    canonicalUnit: z.string().min(1).max(32),
    scaleFactor: positiveDecimalSchema,
    signConvention: z.string().min(1).max(32),
  })
  .strict()
  .refine(hasConsistentFactValue)
  .refine(hasConsistentCurrency);

/** 校验公开治理报表行项目。 */
const financialStatementItemSchema = z
  .object({
    metricCode: metricCodeSchema,
    label: z.string().min(1).max(160),
    value: decimalSchema.nullable(),
    nullReason: z.enum(['NOT_REPORTED', 'NOT_APPLICABLE', 'UPSTREAM_NULL']).nullable(),
    currency: z
      .string()
      .regex(/^[A-Z]{3}$/)
      .nullable(),
    currencyNullReason: currencyNullReasonSchema,
    unit: z.string().min(1).max(32),
  })
  .strict()
  .refine(hasConsistentFactValue)
  .refine(hasConsistentCurrency);

/** 校验内部报表列表页。 */
export const internalFinancialReportPageSchema = z
  .object({
    instrumentId: uuidSchema,
    exchange: exchangeSchema,
    symbol: symbolSchema,
    methodologyCode: methodologyCodeSchema,
    methodologyVersion: z.number().int().positive(),
    items: z.array(internalFinancialReportHeaderSchema).max(50),
    nextCursor: z.string().max(1024).nullable(),
    ...publicationFields,
  })
  .strict();

/** 校验公开报表列表页。 */
export const financialReportPageSchema = z
  .object({
    exchange: exchangeSchema,
    symbol: symbolSchema,
    methodologyCode: methodologyCodeSchema,
    methodologyVersion: z.number().int().positive(),
    items: z.array(financialReportHeaderSchema).max(50),
    nextCursor: z.string().max(1024).nullable(),
    ...publicationFields,
  })
  .strict();

/** 校验内部报表详情页。 */
export const internalFinancialReportDetailSchema = z
  .object({
    report: internalFinancialReportHeaderSchema,
    items: z.array(internalFinancialStatementItemSchema).max(200),
    nextCursor: z.string().max(1024).nullable(),
    ...publicationFields,
  })
  .strict();

/** 校验公开报表详情页。 */
export const financialReportDetailSchema = z
  .object({
    report: financialReportHeaderSchema,
    items: z.array(financialStatementItemSchema).max(200),
    nextCursor: z.string().max(1024).nullable(),
    ...publicationFields,
  })
  .strict();

/** 校验内部供应商或平台财务指标。 */
const internalFinancialMetricSchema = z
  .object({
    metricCode: metricCodeSchema,
    label: z.string().min(1).max(160),
    origin: metricOriginSchema,
    reportPeriod: dateSchema,
    periodBasis: periodBasisSchema,
    statementScope: statementScopeSchema,
    value: decimalSchema,
    unit: z.string().min(1).max(32),
    currency: z
      .string()
      .regex(/^[A-Z]{3}$/)
      .nullable(),
    currencyNullReason: currencyNullReasonSchema,
    methodologyCode: methodologyCodeSchema,
    methodologyVersion: z.number().int().positive(),
    formulaVersion: z.number().int().positive().nullable(),
    effectiveFrom: dateSchema,
    knownFrom: dateTimeSchema,
    knowledgeBasis: knowledgeBasisSchema,
    knowledgeConfidence: knowledgeConfidenceSchema,
    observedAt: dateTimeSchema,
    revision: z.number().int().positive(),
  })
  .strict()
  .refine(hasConsistentCurrency)
  .refine(hasConsistentFormulaVersion);

/** 校验公开供应商或平台财务指标。 */
const financialMetricSchema = z
  .object({
    metricCode: metricCodeSchema,
    label: z.string().min(1).max(160),
    origin: metricOriginSchema,
    reportPeriod: dateSchema,
    periodBasis: periodBasisSchema,
    statementScope: statementScopeSchema,
    value: decimalSchema,
    unit: z.string().min(1).max(32),
    currency: z
      .string()
      .regex(/^[A-Z]{3}$/)
      .nullable(),
    currencyNullReason: currencyNullReasonSchema,
    methodologyCode: methodologyCodeSchema,
    methodologyVersion: z.number().int().positive(),
    formulaVersion: z.number().int().positive().nullable(),
    availableFrom: dateSchema,
    knowledgeBasis: knowledgeBasisSchema,
    knowledgeConfidence: knowledgeConfidenceSchema,
    revision: z.number().int().positive(),
  })
  .strict()
  .refine(hasConsistentCurrency)
  .refine(hasConsistentFormulaVersion);

/** 校验内部财务指标页。 */
export const internalFinancialMetricPageSchema = z
  .object({
    instrumentId: uuidSchema,
    exchange: exchangeSchema,
    symbol: symbolSchema,
    origin: metricOriginSchema,
    methodologyCode: methodologyCodeSchema,
    methodologyVersion: z.number().int().positive(),
    items: z.array(internalFinancialMetricSchema).max(500),
    nextCursor: z.string().max(1024).nullable(),
    ...publicationFields,
  })
  .strict()
  .refine(hasConsistentMetricPage);

/** 校验公开财务指标页。 */
export const financialMetricPageSchema = z
  .object({
    exchange: exchangeSchema,
    symbol: symbolSchema,
    origin: metricOriginSchema,
    methodologyCode: methodologyCodeSchema,
    methodologyVersion: z.number().int().positive(),
    items: z.array(financialMetricSchema).max(500),
    nextCursor: z.string().max(1024).nullable(),
    ...publicationFields,
  })
  .strict()
  .refine(hasConsistentMetricPage);

/** 校验一页内每条指标都绑定页级来源和同一不可变方法学。 */
function hasConsistentMetricPage(value: {
  origin: (typeof FINANCIAL_METRIC_ORIGINS)[number];
  methodologyCode: string;
  methodologyVersion: number;
  items: Array<{
    origin: (typeof FINANCIAL_METRIC_ORIGINS)[number];
    methodologyCode: string;
    methodologyVersion: number;
  }>;
}): boolean {
  for (const item of value.items) {
    if (
      item.origin !== value.origin ||
      item.methodologyCode !== value.methodologyCode ||
      item.methodologyVersion !== value.methodologyVersion
    ) {
      return false;
    }
  }
  return true;
}

/** 校验内部估值观察。 */
const internalValuationObservationSchema = z
  .object({
    observationDate: dateSchema,
    metricCode: z.enum(FINANCIAL_VALUATION_METRICS),
    value: decimalSchema,
    unit: z.string().min(1).max(32),
    currency: z
      .string()
      .regex(/^[A-Z]{3}$/)
      .nullable(),
    currencyNullReason: currencyNullReasonSchema,
    methodologyCode: methodologyCodeSchema,
    methodologyVersion: z.number().int().positive(),
    finality: z.literal('PROVIDER_OBSERVATION'),
    effectiveFrom: dateSchema,
    knownFrom: dateTimeSchema,
    knowledgeBasis: knowledgeBasisSchema,
    knowledgeConfidence: knowledgeConfidenceSchema,
    observedAt: dateTimeSchema,
    revision: z.number().int().positive(),
  })
  .strict()
  .refine(hasConsistentCurrency);

/** 校验公开估值观察。 */
const valuationObservationSchema = z
  .object({
    observationDate: dateSchema,
    metricCode: z.enum(FINANCIAL_VALUATION_METRICS),
    value: decimalSchema,
    unit: z.string().min(1).max(32),
    currency: z
      .string()
      .regex(/^[A-Z]{3}$/)
      .nullable(),
    currencyNullReason: currencyNullReasonSchema,
    methodologyCode: methodologyCodeSchema,
    methodologyVersion: z.number().int().positive(),
    finality: z.literal('PROVIDER_OBSERVATION'),
    availableFrom: dateSchema,
    knowledgeBasis: knowledgeBasisSchema,
    knowledgeConfidence: knowledgeConfidenceSchema,
    revision: z.number().int().positive(),
  })
  .strict()
  .refine(hasConsistentCurrency);

/** 校验内部估值序列页。 */
export const internalValuationPageSchema = z
  .object({
    instrumentId: uuidSchema,
    exchange: exchangeSchema,
    symbol: symbolSchema,
    methodologyCode: methodologyCodeSchema,
    methodologyVersion: z.number().int().positive(),
    items: z.array(internalValuationObservationSchema).max(1000),
    nextCursor: z.string().max(1024).nullable(),
    ...publicationFields,
  })
  .strict();

/** 校验公开估值序列页。 */
export const valuationPageSchema = z
  .object({
    exchange: exchangeSchema,
    symbol: symbolSchema,
    methodologyCode: methodologyCodeSchema,
    methodologyVersion: z.number().int().positive(),
    items: z.array(valuationObservationSchema).max(1000),
    nextCursor: z.string().max(1024).nullable(),
    ...publicationFields,
  })
  .strict();

/** 描述内部报表列表页。 */
export type InternalFinancialReportPage = z.infer<typeof internalFinancialReportPageSchema>;

/** 描述公开报表列表页。 */
export type FinancialReportPage = z.infer<typeof financialReportPageSchema>;

/** 描述内部报表详情页。 */
export type InternalFinancialReportDetail = z.infer<typeof internalFinancialReportDetailSchema>;

/** 描述公开报表详情页。 */
export type FinancialReportDetail = z.infer<typeof financialReportDetailSchema>;

/** 描述内部财务指标页。 */
export type InternalFinancialMetricPage = z.infer<typeof internalFinancialMetricPageSchema>;

/** 描述公开财务指标页。 */
export type FinancialMetricPage = z.infer<typeof financialMetricPageSchema>;

/** 描述内部估值页。 */
export type InternalValuationPage = z.infer<typeof internalValuationPageSchema>;

/** 描述公开估值页。 */
export type ValuationPage = z.infer<typeof valuationPageSchema>;
