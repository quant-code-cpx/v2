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
/** 约束因子、事件与概况共同的已发布版本字段，不伪造行情窗口独有的可用性观测。 */
const referencePublicationFields = {
  exchange: exchangeSchema,
  symbol: symbolSchema,
  dataVersion: z.string().uuid(),
  publishedAt: dateTimeSchema,
  qualityStatus: z.literal('passed'),
  stale: z.literal(false),
};

/** 约束行情窗口的发布可用性与来源观测，允许安全降级返回最后可用窗口。 */
const barPublicationFields = {
  ...referencePublicationFields,
  availability: z.literal('AVAILABLE'),
  observedAt: dateTimeSchema.nullable(),
  reasonCode: z.string().max(80).nullable(),
};

/** 约束可公开复验的精确窗口覆盖与来源批次谱系；不包含服务内证券身份 UUID。 */
const barCoverageLineageFields = {
  coverageVersion: z.string().uuid(),
  publicationKind: z.enum(['DATA', 'ZERO_RECORD_COVERAGE']),
  sourceBatchId: z.string().uuid(),
};

/** 约束因子 publication 的唯一来源投影，使合法空窗口仍可复验 adapter 与上游身份。 */
const adjustmentFactorPublicationSourceSchema = z
  .object({
    sourceBatchId: z.string().uuid(),
    providerId: z.string().min(1).max(100),
    upstreamSource: z.string().min(1).max(100),
    adapterVersion: z.string().min(1).max(64),
  })
  .strict();

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
    ...barPublicationFields,
    ...barCoverageLineageFields,
    period: z.enum(EQUITY_BAR_PERIODS),
    adjustmentMode: z.enum(EQUITY_ADJUSTMENT_MODES),
    adjustAsOf: dateSchema.nullable(),
    factorVersion: z.string().uuid().nullable(),
    formulaVersion: z.literal('cumulative-hfq-v1').nullable(),
    items: z.array(equityBarSchema).max(2000),
    nextCursor: z.string().max(1024).nullable(),
  })
  .strict();

/** 校验保留最后可用版本、同时报告来源暂不可用的安全降级页。 */
const staleEquityBarPageSchema = availableEquityBarPageSchema.extend({
  availability: z.literal('SOURCE_UNAVAILABLE'),
  observedAt: dateTimeSchema,
  reasonCode: z.string().min(1).max(80),
  stale: z.literal(true),
});

/** 校验内部行情页必须为精确覆盖对应的数据或零记录 publication。 */
export const internalEquityBarPageSchema = z
  .union([availableEquityBarPageSchema, staleEquityBarPageSchema])
  .superRefine(assertExactCoverageShape);

/** 公开行情页只保留浏览器可消费的业务字段和已批准的审计谱系。 */
export const equityBarPageSchema = internalEquityBarPageSchema;

/** 拒绝将缺失覆盖、分页错配或空数据 publication 伪装为可用 K 线。 */
function assertExactCoverageShape(
  value: {
    publicationKind: 'DATA' | 'ZERO_RECORD_COVERAGE';
    items: unknown[];
    nextCursor: string | null;
  },
  context: z.RefinementCtx,
): void {
  if (value.publicationKind === 'ZERO_RECORD_COVERAGE') {
    if (value.items.length !== 0) {
      context.addIssue({
        code: 'custom',
        path: ['items'],
        message: 'ZERO_RECORD_COVERAGE requires zero items',
      });
    }
    if (value.nextCursor !== null) {
      context.addIssue({
        code: 'custom',
        path: ['nextCursor'],
        message: 'ZERO_RECORD_COVERAGE cannot have a next cursor',
      });
    }
    return;
  }
  if (value.items.length === 0) {
    context.addIssue({
      code: 'custom',
      path: ['items'],
      message: 'DATA requires at least one item',
    });
  }
}

/** 将严格校验后的内部行情页逐字段投影为公开合同，阻断未来内部字段穿透。 */
export function publicEquityBarPage(input: InternalEquityBarPage): EquityBarPage {
  const common = {
    exchange: input.exchange,
    symbol: input.symbol,
    coverageVersion: input.coverageVersion,
    publicationKind: input.publicationKind,
    sourceBatchId: input.sourceBatchId,
    period: input.period,
    adjustmentMode: input.adjustmentMode,
    adjustAsOf: input.adjustAsOf,
    factorVersion: input.factorVersion,
    formulaVersion: input.formulaVersion,
    dataVersion: input.dataVersion,
    publishedAt: input.publishedAt,
    items: input.items.map((item) => ({
      periodEnd: item.periodEnd,
      open: item.open,
      high: item.high,
      low: item.low,
      close: item.close,
      volumeShares: item.volumeShares,
      amountCny: item.amountCny,
      turnoverRate: item.turnoverRate,
      isFinal: item.isFinal,
      revision: item.revision,
    })),
    nextCursor: input.nextCursor,
  };
  if (input.availability === 'AVAILABLE') {
    return {
      ...common,
      availability: 'AVAILABLE',
      observedAt: input.observedAt,
      reasonCode: input.reasonCode,
      qualityStatus: input.qualityStatus,
      stale: false,
    };
  }
  return {
    ...common,
    availability: 'SOURCE_UNAVAILABLE',
    observedAt: input.observedAt,
    reasonCode: input.reasonCode,
    qualityStatus: input.qualityStatus,
    stale: true,
  };
}

/** 校验稀疏累计后复权因子页。 */
export const equityAdjustmentFactorPageSchema = z
  .object({
    ...referencePublicationFields,
    factorVersion: z.string().uuid(),
    source: adjustmentFactorPublicationSourceSchema,
    items: z
      .array(
        z
          .object({
            effectiveDate: dateSchema,
            cumulativeFactor: nonNegativeDecimalSchema,
            revision: z.number().int().positive(),
            sourceBatchId: z.string().uuid(),
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
    ...referencePublicationFields,
    items: z
      .array(
        z
          .object({
            actionId: z.string().uuid(),
            revision: z.number().int().positive(),
            sourceBatchId: z.string().uuid(),
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
    ...referencePublicationFields,
    identityAsOf: dateSchema,
    revision: z.number().int().positive(),
    sourceBatchId: z.string().uuid(),
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

/** 描述同步服务经过严格合同校验后的内部行情页。 */
export type InternalEquityBarPage = z.infer<typeof internalEquityBarPageSchema>;

/** 描述累计复权因子页。 */
export type EquityAdjustmentFactorPage = z.infer<typeof equityAdjustmentFactorPageSchema>;

/** 描述公司行动页。 */
export type EquityCorporateActionPage = z.infer<typeof equityCorporateActionPageSchema>;

/** 描述公司概况响应。 */
export type EquityCompanyProfile = z.infer<typeof equityCompanyProfileSchema>;
