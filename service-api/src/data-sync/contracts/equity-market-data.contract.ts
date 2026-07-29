import { z } from 'zod';

import { EQUITY_EXCHANGES } from './equity-instrument.contract.js';

/** 约束个股行情三个独立上游物理周期。 */
export const EQUITY_BAR_PERIODS = ['1d', '1w', '1mo'] as const;

/** 约束公开行情支持的复权模式。 */
export const EQUITY_ADJUSTMENT_MODES = ['none', 'qfq', 'hfq'] as const;

const dateSchema = z.string().date();
const dateTimeSchema = z.string().datetime({ offset: true });
const nonNegativeDecimalSchema = z.string().regex(/^[0-9]+(?:\.[0-9]+)?$/);
const exchangeSchema = z.enum(EQUITY_EXCHANGES);
const symbolSchema = z.string().regex(/^[0-9]{6}$/);
const publicationFields = {
  exchange: exchangeSchema,
  symbol: symbolSchema,
  dataVersion: z.string().uuid(),
  publishedAt: dateTimeSchema,
  availability: z.literal('AVAILABLE'),
  observedAt: dateTimeSchema.nullable(),
  reasonCode: z.string().max(80).nullable(),
  qualityStatus: z.literal('passed'),
  stale: z.literal(false),
};

/** 校验一条日、周或月未复权或查询时复权的行情。 */
const equityBarSchema = z
  .object({
    periodEnd: dateSchema,
    open: nonNegativeDecimalSchema,
    high: nonNegativeDecimalSchema,
    low: nonNegativeDecimalSchema,
    close: nonNegativeDecimalSchema,
    volumeShares: z.string().regex(/^[0-9]+$/),
    amountCny: nonNegativeDecimalSchema,
    turnoverRate: nonNegativeDecimalSchema.nullable(),
    isFinal: z.boolean(),
    revision: z.number().int().positive(),
  })
  .strict();

/** 校验一个已发布行情页及复权方法身份。 */
const availableEquityBarPageSchema = z
  .object({
    ...publicationFields,
    period: z.enum(EQUITY_BAR_PERIODS),
    adjustmentMode: z.enum(EQUITY_ADJUSTMENT_MODES),
    adjustAsOf: dateSchema.nullable(),
    factorVersion: z.string().uuid().nullable(),
    formulaVersion: z.literal('cumulative-hfq-v1').nullable(),
    items: z.array(equityBarSchema).max(2000),
    nextCursor: z.string().max(1024).nullable(),
  })
  .strict();

/** 校验来源空集或暂不可用时返回的成功空页，避免伪造业务事实。 */
const emptyEquityBarPageSchema = z
  .object({
    exchange: exchangeSchema,
    symbol: symbolSchema,
    period: z.enum(EQUITY_BAR_PERIODS),
    adjustmentMode: z.enum(EQUITY_ADJUSTMENT_MODES),
    adjustAsOf: dateSchema.nullable(),
    factorVersion: z.null(),
    formulaVersion: z.null(),
    dataVersion: z.null(),
    publishedAt: z.null(),
    availability: z.enum(['EMPTY', 'SOURCE_UNAVAILABLE']),
    observedAt: dateTimeSchema,
    reasonCode: z.string().min(1).max(80),
    qualityStatus: z.null(),
    stale: z.literal(false),
    items: z.array(equityBarSchema).length(0),
    nextCursor: z.null(),
  })
  .strict();

/** 校验保留最后可用版本、同时报告来源暂不可用的安全降级页。 */
const staleEquityBarPageSchema = availableEquityBarPageSchema.extend({
  availability: z.literal('SOURCE_UNAVAILABLE'),
  observedAt: dateTimeSchema,
  reasonCode: z.string().min(1).max(80),
  stale: z.literal(true),
});

/** 校验可发布事实页、成功空页或保留历史版本的安全降级页。 */
export const equityBarPageSchema = z.union([
  availableEquityBarPageSchema,
  emptyEquityBarPageSchema,
  staleEquityBarPageSchema,
]);

/** 校验稀疏累计后复权因子页。 */
export const equityAdjustmentFactorPageSchema = z
  .object({
    ...publicationFields,
    factorVersion: z.string().uuid(),
    items: z
      .array(
        z
          .object({
            effectiveDate: dateSchema,
            cumulativeFactor: nonNegativeDecimalSchema,
            revision: z.number().int().positive(),
          })
          .strict(),
      )
      .max(500),
    nextCursor: z.string().max(1024).nullable(),
  })
  .strict();

/** 校验公司行动当前 revision 页。 */
export const equityCorporateActionPageSchema = z
  .object({
    ...publicationFields,
    items: z
      .array(
        z
          .object({
            actionId: z.string().uuid(),
            revision: z.number().int().positive(),
            reportPeriod: dateSchema,
            status: z.string().min(1).max(80),
            announcementDate: dateSchema.nullable(),
            recordDate: dateSchema.nullable(),
            exDate: dateSchema.nullable(),
            cashDividendPer10: nonNegativeDecimalSchema.nullable(),
            bonusSharesPer10: nonNegativeDecimalSchema.nullable(),
            transferSharesPer10: nonNegativeDecimalSchema.nullable(),
          })
          .strict(),
      )
      .max(100),
    nextCursor: z.string().max(1024).nullable(),
  })
  .strict();

/** 校验当前已发布公司概况。 */
export const equityCompanyProfileSchema = z
  .object({
    ...publicationFields,
    revision: z.number().int().positive(),
    profile: z
      .object({
        companyName: z.string().min(1).max(300),
        englishName: z.string().max(500).nullable(),
        industry: z.string().max(300).nullable(),
        legalRepresentative: z.string().max(160).nullable(),
        establishedOn: dateSchema.nullable(),
        website: z.string().max(1000).nullable(),
        email: z.string().max(500).nullable(),
        phone: z.string().max(300).nullable(),
        registeredAddress: z.string().nullable(),
        officeAddress: z.string().nullable(),
        mainBusiness: z.string().nullable(),
        businessScope: z.string().nullable(),
        summary: z.string().nullable(),
      })
      .strict(),
  })
  .strict();

/** 描述个股行情页。 */
export type EquityBarPage = z.infer<typeof equityBarPageSchema>;

/** 描述累计复权因子页。 */
export type EquityAdjustmentFactorPage = z.infer<typeof equityAdjustmentFactorPageSchema>;

/** 描述公司行动页。 */
export type EquityCorporateActionPage = z.infer<typeof equityCorporateActionPageSchema>;

/** 描述公司概况响应。 */
export type EquityCompanyProfile = z.infer<typeof equityCompanyProfileSchema>;
