import { z } from 'zod';

/** 约束公开 API 与内部合同共同支持的板块分类体系。 */
export const SECTOR_SCHEMES = ['eastmoney.industry', 'eastmoney.concept'] as const;

/** 表示不丢失精度的非负十进制 JSON 字符串。 */
const nonNegativeDecimalString = z.string().regex(/^[0-9]+(\.[0-9]+)?$/);

/** 表示可正可负的不丢失精度十进制 JSON 字符串。 */
const decimalString = z.string().regex(/^-?[0-9]+(\.[0-9]+)?$/);
const membershipDateTimeSchema = z.string().datetime({ offset: true });
const membershipListingStatusSchema = z.enum(['LISTED', 'SUSPENDED', 'DELISTED']);

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

/** 校验固定 release 的观测语义、覆盖范围和公开质量统计。 */
const membershipReleaseSchema = z
  .object({
    requestedAsOf: membershipDateTimeSchema.nullable(),
    resolvedAsOf: membershipDateTimeSchema,
    coverageStart: membershipDateTimeSchema,
    membershipSemantics: z.literal('observed'),
    qualityStatus: z.enum(['passed', 'warned']),
    identityCoveragePercent: z.literal('100'),
    excludedIdentityCount: z.literal(0),
    carriedForwardSectorCount: z.number().int().nonnegative(),
    dataVersion: z.string().uuid(),
    publishedAt: membershipDateTimeSchema,
  })
  .strict();

/** 校验同步服务内部使用的成分板块身份，稳定 UUID 仅用于服务间关联。 */
const internalMembershipSectorSchema = z
  .object({
    sectorId: z.string().uuid(),
    scheme: z.enum(SECTOR_SCHEMES),
    code: z.string().min(1).max(64),
    name: z.string().min(1).max(200),
  })
  .strict();

/** 校验同步服务内部返回的 verified 成分，禁止 PENDING 或数据库数值主键。 */
const internalMembershipConstituentSchema = z
  .object({
    instrumentId: z.string().uuid(),
    exchange: z.enum(['SSE', 'SZSE', 'BSE']),
    symbol: z.string().regex(/^[0-9]{6}$/),
    name: z.string().min(1).max(200),
    listingStatus: membershipListingStatusSchema,
    observedFrom: membershipDateTimeSchema,
    observedTo: membershipDateTimeSchema.nullable(),
  })
  .strict();

/** 校验同步服务内部反向读取的一条板块观测归属。 */
const internalEquityMembershipSchema = z
  .object({
    sectorId: z.string().uuid(),
    scheme: z.enum(SECTOR_SCHEMES),
    code: z.string().min(1).max(64),
    name: z.string().min(1).max(200),
    observedFrom: membershipDateTimeSchema,
    observedTo: membershipDateTimeSchema.nullable(),
    snapshotObservedAt: membershipDateTimeSchema,
    carriedForward: z.boolean(),
  })
  .strict();

/** 校验内部板块到成分页，所有成分均来自同一不可变 release。 */
export const internalSectorConstituentPageSchema = z
  .object({
    sector: internalMembershipSectorSchema,
    release: membershipReleaseSchema,
    snapshotObservedAt: membershipDateTimeSchema,
    carriedForward: z.boolean(),
    items: z.array(internalMembershipConstituentSchema).max(500),
    nextCursor: z.string().max(1024).nullable(),
  })
  .strict();

/** 校验内部证券到板块分页，证券 UUID 只允许停留在服务间防腐层。 */
export const internalEquitySectorPageSchema = z
  .object({
    equity: z
      .object({
        instrumentId: z.string().uuid(),
        exchange: z.enum(['SSE', 'SZSE', 'BSE']),
        symbol: z.string().regex(/^[0-9]{6}$/),
        name: z.string().min(1).max(200),
        listingStatus: membershipListingStatusSchema,
      })
      .strict(),
    scheme: z.enum(SECTOR_SCHEMES),
    release: membershipReleaseSchema,
    items: z.array(internalEquityMembershipSchema).max(500),
    nextCursor: z.string().max(1024).nullable(),
  })
  .strict();

/** 描述公开板块到成分页，已移除 sectorId 与 instrumentId。 */
export type SectorConstituentPage = {
  sector: {
    scheme: (typeof SECTOR_SCHEMES)[number];
    code: string;
    name: string;
  };
  release: z.infer<typeof membershipReleaseSchema>;
  snapshotObservedAt: string;
  carriedForward: boolean;
  items: Array<{
    exchange: 'SSE' | 'SZSE' | 'BSE';
    symbol: string;
    name: string;
    listingStatus: 'LISTED' | 'SUSPENDED' | 'DELISTED';
    observedFrom: string;
    observedTo: string | null;
  }>;
  nextCursor: string | null;
};

/** 描述公开证券到板块分页，已移除内部稳定 UUID。 */
export type EquitySectorPage = {
  equity: {
    exchange: 'SSE' | 'SZSE' | 'BSE';
    symbol: string;
    name: string;
    listingStatus: 'LISTED' | 'SUSPENDED' | 'DELISTED';
  };
  scheme: (typeof SECTOR_SCHEMES)[number];
  release: z.infer<typeof membershipReleaseSchema>;
  items: Array<{
    scheme: (typeof SECTOR_SCHEMES)[number];
    code: string;
    name: string;
    observedFrom: string;
    observedTo: string | null;
    snapshotObservedAt: string;
    carriedForward: boolean;
  }>;
  nextCursor: string | null;
};

/** 描述内部板块到成分页，供防腐 client 严格校验。 */
export type InternalSectorConstituentPage = z.infer<typeof internalSectorConstituentPageSchema>;

/** 描述内部证券到板块分页，供防腐 client 严格校验。 */
export type InternalEquitySectorPage = z.infer<typeof internalEquitySectorPageSchema>;
