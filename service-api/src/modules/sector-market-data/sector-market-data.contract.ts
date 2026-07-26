import { z } from 'zod';

/** 约束公开 API 与内部合同共同支持的板块分类体系。 */
export const SECTOR_SCHEMES = ['eastmoney.industry', 'eastmoney.concept'] as const;

/** 表示不丢失精度的非负十进制 JSON 字符串。 */
const nonNegativeDecimalString = z.string().regex(/^[0-9]+(\.[0-9]+)?$/);

/** 表示可正可负的不丢失精度十进制 JSON 字符串。 */
const decimalString = z.string().regex(/^-?[0-9]+(\.[0-9]+)?$/);

/** 校验同步服务内部返回的完整板块身份，含仅内部使用的稳定 UUID。 */
export const internalSectorSchema = z
  .object({
    sectorId: z.string().uuid(),
    scheme: z.enum(SECTOR_SCHEMES),
    code: z.string().min(1).max(64),
    name: z.string().min(1).max(200),
    dataVersion: z.string().uuid(),
    publishedAt: z.string().min(1),
  })
  .strict();

/** 校验公开 API 允许投影的板块身份，不包含内部 UUID。 */
export const sectorSchema = internalSectorSchema.omit({ sectorId: true });

/** 校验同步服务原样提供的独立上游周期 K 线。 */
export const sectorBarSchema = z
  .object({
    periodEnd: z.string().date(),
    open: nonNegativeDecimalString,
    high: nonNegativeDecimalString,
    low: nonNegativeDecimalString,
    close: nonNegativeDecimalString,
    volumeValue: nonNegativeDecimalString,
    volumeUnit: z.literal('provider_native'),
    amountCny: nonNegativeDecimalString,
    amplitudePercent: nonNegativeDecimalString.nullable(),
    changePercent: decimalString.nullable(),
    changeAmount: decimalString.nullable(),
    turnoverPercent: nonNegativeDecimalString.nullable(),
    isFinal: z.boolean(),
  })
  .strict();

/** 校验同步服务的目录分页响应。 */
export const internalSectorPageSchema = z
  .object({
    items: z.array(internalSectorSchema).max(100),
    nextCursor: z.string().max(1024).nullable(),
    dataVersion: z.string().uuid(),
    publishedAt: z.string().min(1),
  })
  .strict();

/** 校验同步服务的 K 线分页响应。 */
export const internalSectorBarPageSchema = z
  .object({
    sector: internalSectorSchema,
    period: z.enum(['1d', '1w', '1mo']),
    dataVersion: z.string().uuid(),
    publishedAt: z.string().min(1),
    items: z.array(sectorBarSchema).max(1000),
    nextCursor: z.string().max(1024).nullable(),
  })
  .strict();

/** 描述公开列表使用的板块身份类型。 */
export type Sector = z.infer<typeof sectorSchema>;

/** 描述仅用于服务间边界校验、携带稳定内部 UUID 的板块身份类型。 */
export type InternalSector = z.infer<typeof internalSectorSchema>;

/** 描述公开 API 返回的一条独立上游 K 线类型。 */
export type SectorBar = z.infer<typeof sectorBarSchema>;

/** 描述同步服务返回的目录分页类型。 */
export type InternalSectorPage = z.infer<typeof internalSectorPageSchema>;

/** 描述同步服务返回的 K 线分页类型。 */
export type InternalSectorBarPage = z.infer<typeof internalSectorBarPageSchema>;

/** 描述公开 API 返回的目录分页类型。 */
export type SectorPage = {
  items: Sector[];
  nextCursor: string | null;
  dataVersion: string;
  publishedAt: string;
};

/** 描述公开 API 返回的 K 线分页类型。 */
export type SectorBarPage = {
  sector: Sector;
  period: '1d' | '1w' | '1mo';
  dataVersion: string;
  publishedAt: string;
  items: SectorBar[];
  nextCursor: string | null;
};
