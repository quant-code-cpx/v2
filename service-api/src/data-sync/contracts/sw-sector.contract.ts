import { z } from 'zod';

/** 表示可正可负且不丢失精度的十进制 JSON 字符串。 */
const decimalString = z.string().regex(/^-?[0-9]+(\.[0-9]+)?$/);

/** 校验公开可见的方法学来源和语义摘要。 */
export const swMethodologySchema = z
  .object({
    code: z.string().min(1).max(80),
    version: z.number().int().positive(),
    status: z.literal('source_reported'),
    upstreamSource: z.string().min(1).max(120),
    semanticSpecSha256: z.string().regex(/^[0-9a-f]{64}$/),
  })
  .strict();

/** 校验 taxonomy 或估值读取绑定的不可变发布。 */
export const swReleaseSchema = z
  .object({
    snapshotDate: z.string().date(),
    dataVersion: z.string().uuid(),
    publishedAt: z.string().datetime({ offset: true }),
    qualityStatus: z.enum(['passed', 'warned']),
    rowCount: z.number().int().positive(),
    methodology: swMethodologySchema,
  })
  .strict();

/** 校验一个申万节点、直接父级和当前知识修订。 */
export const swIndustryNodeSchema = z
  .object({
    code: z.string().regex(/^[0-9]{6}\.SI$/),
    name: z.string().min(1).max(200),
    level: z.number().int().min(1).max(3),
    parentCode: z
      .string()
      .regex(/^[0-9]{6}\.SI$/)
      .nullable(),
    componentCount: z.number().int().nonnegative(),
    revision: z.number().int().positive(),
  })
  .strict();

/** 校验同步服务返回的申万 taxonomy 分页。 */
export const swIndustryPageSchema = z
  .object({
    scheme: z.literal('sw.industry'),
    release: swReleaseSchema,
    items: z.array(swIndustryNodeSchema).max(500),
    nextCursor: z.string().max(1024).nullable(),
  })
  .strict();

/** 校验一个节点及其冻结发布中的根到直接父级闭包。 */
export const swIndustryResourceSchema = z
  .object({
    scheme: z.literal('sw.industry'),
    release: swReleaseSchema,
    industry: swIndustryNodeSchema,
    ancestors: z.array(swIndustryNodeSchema).max(2),
  })
  .strict();

/** 校验供应商观察估值；股息率单位固定为一比一比例。 */
export const swIndustryValuationSchema = swIndustryNodeSchema
  .omit({ revision: true })
  .extend({
    snapshotDate: z.string().date(),
    staticPe: decimalString.nullable(),
    ttmPe: decimalString.nullable(),
    pb: decimalString.nullable(),
    dividendYieldRatio: decimalString.nullable(),
    finality: z.literal('PROVIDER_OBSERVATION'),
    valuationRevision: z.number().int().positive(),
  })
  .strict();

/** 校验同步服务返回的申万估值分页。 */
export const swIndustryValuationPageSchema = z
  .object({
    scheme: z.literal('sw.industry'),
    release: swReleaseSchema,
    items: z.array(swIndustryValuationSchema).max(500),
    nextCursor: z.string().max(1024).nullable(),
  })
  .strict();

/** 描述公开 taxonomy 分页响应。 */
export type SwIndustryPage = z.infer<typeof swIndustryPageSchema>;

/** 描述公开单节点与父级闭包响应。 */
export type SwIndustryResource = z.infer<typeof swIndustryResourceSchema>;

/** 描述公开估值分页响应。 */
export type SwIndustryValuationPage = z.infer<typeof swIndustryValuationPageSchema>;
