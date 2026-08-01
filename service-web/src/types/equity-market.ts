import { z } from "zod";

/** 股票中心公开支持的交易所。 */
export const equityExchangeSchema = z.enum(["SSE", "SZSE", "BSE"]);

/** 上市生命周期状态；`SUSPENDED` 只表示暂停上市。 */
export const equityListingStatusSchema = z.enum(["LISTED", "SUSPENDED", "DELISTED"]);

/** 目标 EOD 的普通交易状态，与上市生命周期相互独立。 */
export const equityTradingStatusSchema = z.enum([
  "TRADED",
  "TRADE_SUSPENDED",
  "NO_SESSION",
  "NOT_APPLICABLE",
  "UNKNOWN",
]);

/** 页面可显示的数据可用性状态。 */
export const equityAvailabilitySchema = z.enum([
  "AVAILABLE",
  "EMPTY",
  "UNAVAILABLE",
  "PARTIAL",
  "SOURCE_UNAVAILABLE",
]);

/** 已发布数据的独立新鲜度。 */
export const equityFreshnessSchema = z.enum(["FRESH", "STALE", "UNKNOWN"]);

/** 原因化空值，禁止把不可用数据替换为数字零。 */
export const equityNullReasonSchema = z.enum([
  "NOT_APPLICABLE",
  "LEGITIMATE_EMPTY",
  "NO_PRIOR_VALUE",
  "NOT_COVERED",
]);

/** discovery 允许的稳定排序字段。 */
export const equitySearchSortFieldSchema = z.enum([
  "symbol",
  "name",
  "close",
  "changePercent",
  "amountCny",
  "turnoverRate",
  "totalMarketCap",
  "floatMarketCap",
  "peTtm",
  "pb",
]);

const decimalSchema = z.string().regex(/^-?\d+(?:\.\d+)?$/);
const nonNegativeDecimalSchema = z.string().regex(/^\d+(?:\.\d+)?$/);
const integerStringSchema = z.string().regex(/^\d+$/);
const dateSchema = z.string().regex(/^\d{4}-\d{2}-\d{2}$/);
const dateTimeSchema = z.string().datetime({ offset: true });
const uuidSchema = z.string().uuid();
const nullableReasonSchema = z
  .string()
  .regex(/^[A-Z][A-Z0-9_]{0,79}$/)
  .nullish();

/** 描述响应绑定的不可变发布版本。 */
export const equityReleaseSchema = z
  .object({
    dataVersion: uuidSchema,
    publishedAt: dateTimeSchema,
    effectiveAsOf: dateSchema.nullish(),
    knowledgeCutoff: dateTimeSchema.nullish(),
    qualityStatus: z.enum(["passed", "warning", "failed", "PASSED", "WARNING", "FAILED"]),
    completeness: z.enum(["FULL", "PARTIAL"]).optional(),
  })
  .passthrough();

/** discovery publication 必须显式声明 FULL 或 PARTIAL，浏览器不得从缺值推断。 */
const equitySearchReleaseSchema = equityReleaseSchema.extend({
  completeness: z.enum(["FULL", "PARTIAL"]),
});

/** 描述 response 级组件来源，避免在每只证券上重复方法学。 */
export const equityComponentSchema = z
  .object({
    family: z.string().min(1).max(120),
    dataVersion: uuidSchema.nullish(),
    availability: equityAvailabilitySchema,
    methodology: z
      .object({
        code: z.string().min(1).max(120),
        version: z.string().min(1).max(80),
      })
      .passthrough()
      .nullish(),
    sourceLabel: z.string().min(1).max(200).nullish(),
  })
  .passthrough();

/** 描述 discovery 可筛选、可排序和空值口径能力。 */
export const equityCapabilitySchema = z
  .object({
    sortFields: z.array(equitySearchSortFieldSchema).max(11),
    columns: z.array(z.string().min(1).max(80)).max(24),
    maxLimit: z.number().int().min(1).max(100),
  })
  .passthrough();

/** 股票列表中一只证券的公开路由身份。 */
export const equitySearchIdentitySchema = z
  .object({
    exchange: equityExchangeSchema,
    symbol: z.string().regex(/^\d{6}$/),
    name: z.string().min(1).max(300),
    identityAsOf: dateSchema,
  })
  .passthrough();

/** 股票列表独立展示的上市与交易状态。 */
export const equitySearchStatusesSchema = z
  .object({
    listingStatus: equityListingStatusSchema,
    tradingStatus: equityTradingStatusSchema,
    tradingStatusReason: z.string().min(1).max(160).nullish(),
    listedOn: dateSchema.nullish(),
    delistedOn: dateSchema.nullish(),
  })
  .passthrough();

/** 股票列表最近已发布的非实时 EOD 行情。 */
export const equitySearchMarketSchema = z
  .object({
    tradeDate: dateSchema.nullish(),
    close: nonNegativeDecimalSchema.nullish(),
    previousClose: nonNegativeDecimalSchema.nullish(),
    changeAmount: decimalSchema.nullish(),
    changePercent: decimalSchema.nullish(),
    volumeShares: nonNegativeDecimalSchema.nullish(),
    amountCny: nonNegativeDecimalSchema.nullish(),
    turnoverRate: nonNegativeDecimalSchema.nullish(),
    currency: z.literal("CNY"),
    nullReason: nullableReasonSchema,
  })
  .passthrough();

/** 股票列表按未复权收盘价与生效股本计算的市值。 */
export const equitySearchCapitalizationSchema = z
  .object({
    effectiveOn: dateSchema.nullish(),
    totalShares: nonNegativeDecimalSchema.nullish(),
    listedTradableAShares: nonNegativeDecimalSchema.nullish(),
    totalMarketCapCny: nonNegativeDecimalSchema.nullish(),
    floatMarketCapCny: nonNegativeDecimalSchema.nullish(),
    currency: z.literal("CNY"),
    methodology: z
      .object({
        code: z.string().min(1).max(120),
        version: z.string().min(1).max(80),
      })
      .passthrough()
      .nullish(),
    nullReason: nullableReasonSchema,
  })
  .passthrough();

/** 股票列表已冻结方法学的供应商估值观察。 */
export const equitySearchValuationSchema = z
  .object({
    tradeDate: dateSchema.nullish(),
    peTtm: decimalSchema.nullish(),
    pb: decimalSchema.nullish(),
    psTtm: decimalSchema.nullish(),
    sourceLabel: z.string().min(1).max(160).nullish(),
    methodology: z
      .object({
        code: z.string().min(1).max(120),
        version: z.string().min(1).max(80),
      })
      .passthrough()
      .nullish(),
    nullReason: nullableReasonSchema,
  })
  .passthrough();

/** 股票列表 Eastmoney order-size MAIN 日频资金流投影。 */
export const equitySearchMoneyFlowSchema = z
  .object({
    tradeDate: dateSchema.nullish(),
    netAmountCny: decimalSchema.nullish(),
    netRatio: decimalSchema.nullish(),
    sourceLabel: z.string().min(1).max(160).nullish(),
    methodology: z
      .object({
        code: z.string().min(1).max(120),
        version: z.string().min(1).max(80),
      })
      .passthrough()
      .nullish(),
    nullReason: nullableReasonSchema,
  })
  .passthrough();

/** 股票列表同一 release 内的一条行业、概念或申万归属。 */
export const equitySearchMembershipSchema = z
  .object({
    scheme: z.enum([
      "EASTMONEY_INDUSTRY",
      "EASTMONEY_CONCEPT",
      "SW2021_L1",
      "SW2021_L2",
      "SW2021_L3",
    ]),
    code: z.string().min(1).max(80),
    name: z.string().min(1).max(200),
    level: z.number().int().min(1).max(3).nullable().optional(),
    observedOn: dateSchema,
  })
  .passthrough();

/** 股票列表一行同一 discovery dataVersion 的完整公开投影。 */
export const equitySearchRecordSchema = z
  .object({
    identity: equitySearchIdentitySchema,
    statuses: equitySearchStatusesSchema,
    market: equitySearchMarketSchema,
    capitalization: equitySearchCapitalizationSchema,
    valuation: equitySearchValuationSchema,
    moneyFlow: equitySearchMoneyFlowSchema,
    memberships: z.array(equitySearchMembershipSchema).max(200),
  })
  .passthrough();

/** 股票中心 discovery 搜索响应。 */
export const equitySearchResponseSchema = z
  .object({
    availability: z.enum(["AVAILABLE", "UNAVAILABLE"]),
    reasonCode: nullableReasonSchema,
    release: equitySearchReleaseSchema.nullable(),
    components: z.array(equityComponentSchema).max(64),
    capabilities: equityCapabilitySchema,
    records: z.array(equitySearchRecordSchema).max(100),
    page: z
      .object({
        nextCursor: z.string().max(1024).nullable(),
        limit: z.number().int().min(1).max(100),
      })
      .passthrough(),
  })
  .passthrough()
  /** 校验搜索 envelope 的 availability 与 publication/事实不变量。 */
  .superRefine((value, context) => {
    // AVAILABLE 必须绑定 publication；UNAVAILABLE 不能携带可误消费的记录。
    if (value.availability === "AVAILABLE" && value.release === null) {
      context.addIssue({
        code: "custom",
        path: ["release"],
        message: "available search requires release",
      });
    }
    if (
      value.availability === "UNAVAILABLE" &&
      (value.release !== null || value.records.length > 0)
    ) {
      context.addIssue({
        code: "custom",
        path: ["availability"],
        message: "unavailable search cannot contain release or records",
      });
    }
  });

/** 事件页签支持的五类受控证券事件。 */
export const equityEventFamilySchema = z.enum([
  "CORPORATE_ACTION",
  "EARNINGS_FORECAST",
  "EARNINGS_EXPRESS",
  "DRAGON_TIGER",
  "BLOCK_TRADE",
]);

/** 一条不泄漏内部 UUID 的证券事件。 */
export const equityEventSchema = z
  .object({
    eventRef: z.string().regex(/^evt_[A-Za-z0-9_-]{43}$/),
    family: equityEventFamilySchema,
    kind: z.string().min(1).max(120),
    stage: z.string().min(1).max(120).nullish(),
    status: z.string().min(1).max(120).nullish(),
    occurredOn: dateSchema.nullish(),
    announcedOn: dateSchema.nullish(),
    reportPeriod: dateSchema.nullish(),
    title: z.string().min(1).max(500).nullish(),
    sourceLabel: z.string().min(1).max(160).nullish(),
    dataVersion: uuidSchema,
    facts: z
      .array(
        z
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
          .passthrough(),
      )
      .max(64),
  })
  .passthrough();

/** 证券事件分页响应。 */
export const equityEventPageSchema = z
  .object({
    availability: z.enum(["AVAILABLE", "UNAVAILABLE"]),
    reasonCode: nullableReasonSchema,
    release: equityReleaseSchema.nullable(),
    events: z.array(equityEventSchema).max(100),
    page: z
      .object({
        nextCursor: z.string().max(1024).nullable(),
        limit: z.number().int().min(1).max(100),
      })
      .passthrough(),
  })
  .passthrough()
  /** 校验事件 envelope 的 availability 与 publication/事实不变量。 */
  .superRefine((value, context) => {
    // 事件可用性与 publication/事实集合必须保持同一不变量。
    if (value.availability === "AVAILABLE" && value.release === null) {
      context.addIssue({
        code: "custom",
        path: ["release"],
        message: "available events require release",
      });
    }
    if (
      value.availability === "UNAVAILABLE" &&
      (value.release !== null || value.events.length > 0)
    ) {
      context.addIssue({
        code: "custom",
        path: ["availability"],
        message: "unavailable events cannot contain release or events",
      });
    }
  });

/** 详情页单个数据集的可用性、发布与方法学状态。 */
export const equityDatasetStatusSchema = z
  .object({
    family: z.string().min(1).max(120),
    dataset: z.string().regex(/^[a-z0-9][a-z0-9._-]{0,159}$/),
    availability: equityAvailabilitySchema,
    freshness: equityFreshnessSchema,
    dataVersion: uuidSchema.nullish(),
    publishedAt: dateTimeSchema.nullish(),
    effectiveAsOf: dateSchema.nullish(),
    knowledgeCutoff: dateTimeSchema.nullish(),
    sourceLabel: z.string().min(1).max(200).nullish(),
    methodology: z
      .object({
        code: z.string().min(1).max(120),
        version: z.string().min(1).max(80),
      })
      .passthrough()
      .nullish(),
    reasonCode: nullableReasonSchema,
    retryable: z.boolean(),
  })
  .passthrough();

/** 详情页一次读取全部元数据状态的响应。 */
export const equityDataStatusResponseSchema = z
  .object({
    identity: equitySearchIdentitySchema.nullable().optional(),
    datasets: z.array(equityDatasetStatusSchema).max(18),
  })
  .passthrough();

/** 既有轻量目录和详情返回的双时态公开身份。 */
const temporalIdentitySchema = z
  .object({
    identifier: z
      .object({
        exchange: equityExchangeSchema,
        symbol: z.string().regex(/^\d{6}$/),
        effectiveFrom: dateSchema,
        effectiveTo: dateSchema.nullable(),
        datePrecision: z.enum(["OFFICIAL_DATE", "OBSERVATION_DATE"]),
        knownFrom: dateTimeSchema,
        observedAt: dateTimeSchema,
      })
      .strict(),
    name: z
      .object({
        value: z.string().min(1).max(200),
        effectiveFrom: dateSchema,
        effectiveTo: dateSchema.nullable(),
        datePrecision: z.enum(["OFFICIAL_DATE", "OBSERVATION_DATE"]),
        knownFrom: dateTimeSchema,
        observedAt: dateTimeSchema,
      })
      .strict(),
    listing: z
      .object({
        status: equityListingStatusSchema,
        listedOn: dateSchema.nullable(),
        delistedOn: dateSchema.nullable(),
        effectiveFrom: dateSchema,
        effectiveTo: dateSchema.nullable(),
        datePrecision: z.enum(["OFFICIAL_DATE", "OBSERVATION_DATE"]),
        knownFrom: dateTimeSchema,
        observedAt: dateTimeSchema,
      })
      .strict(),
  })
  .strict();

/** 既有单证券详情公开响应。 */
export const equityIdentityDetailSchema = temporalIdentitySchema
  .extend({
    dataVersion: uuidSchema,
    publishedAt: dateTimeSchema,
    effectiveAsOf: dateSchema,
    knowledgeCutoff: dateTimeSchema,
  })
  .strict();

/** 既有轻量证券目录响应，用于旧 symbol-only 路由迁移。 */
export const equityIdentityPageSchema = z
  .object({
    items: z.array(temporalIdentitySchema).max(100),
    nextCursor: z.string().max(1024).nullable(),
    dataVersion: uuidSchema,
    publishedAt: dateTimeSchema,
    effectiveAsOf: dateSchema,
    knowledgeCutoff: dateTimeSchema,
    publicationScope: z.enum(["SSE", "SZSE", "BSE", "CN_A_STABLE"]),
  })
  .strict();

/** 既有上市生命周期历史响应，普通停牌不会出现在此时间线。 */
export const equityListingStatusHistoryPageSchema = z
  .object({
    exchange: equityExchangeSchema,
    symbol: z.string().regex(/^\d{6}$/),
    items: z
      .array(
        z
          .object({
            status: equityListingStatusSchema,
            effectiveFrom: dateSchema,
            effectiveTo: dateSchema.nullable(),
            effectiveDatePrecision: z.enum(["OFFICIAL_DATE", "OBSERVATION_DATE"]),
            knownFrom: dateTimeSchema,
            knownTo: dateTimeSchema.nullable(),
            observedAt: dateTimeSchema,
          })
          .strict(),
      )
      .max(100),
    nextCursor: z.string().max(1024).nullable(),
    dataVersion: uuidSchema,
    publishedAt: dateTimeSchema,
    knowledgeCutoff: dateTimeSchema,
  })
  .strict();

/** 约束可公开复验的精确 K 线覆盖与来源批次谱系，不包含服务内证券身份 UUID。 */
const equityBarCoverageLineageFields = {
  coverageVersion: uuidSchema,
  publicationKind: z.enum(["DATA", "ZERO_RECORD_COVERAGE"]),
  sourceBatchId: uuidSchema,
};

/** 复用已发布日、周、月行情页的公共业务字段。 */
const equityBarPageFields = {
  exchange: equityExchangeSchema,
  symbol: z.string().regex(/^\d{6}$/),
  ...equityBarCoverageLineageFields,
  period: z.enum(["1d", "1w", "1mo"]),
  adjustmentMode: z.enum(["none", "qfq", "hfq"]),
  adjustAsOf: dateSchema.nullable(),
  factorVersion: uuidSchema.nullable(),
  formulaVersion: z.literal("cumulative-hfq-v1").nullable(),
  dataVersion: uuidSchema,
  publishedAt: dateTimeSchema,
  qualityStatus: z.literal("passed"),
  items: z
    .array(
      z
        .object({
          periodEnd: dateSchema,
          open: nonNegativeDecimalSchema,
          high: nonNegativeDecimalSchema,
          low: nonNegativeDecimalSchema,
          close: nonNegativeDecimalSchema,
          volumeShares: integerStringSchema,
          amountCny: nonNegativeDecimalSchema,
          turnoverRate: nonNegativeDecimalSchema.nullable(),
          isFinal: z.boolean(),
          revision: z.number().int().positive(),
        })
        .strict(),
    )
    .max(2000),
  nextCursor: z.string().max(1024).nullable(),
};

/** 约束当前精确覆盖可读取的正常行情 publication。 */
const availableEquityBarPageSchema = z
  .object({
    ...equityBarPageFields,
    availability: z.literal("AVAILABLE"),
    observedAt: dateTimeSchema.nullable(),
    reasonCode: z.string().max(80).nullable(),
    stale: z.literal(false),
  })
  .strict();

/** 约束保留最后合格精确覆盖、同时报告来源暂不可用的只读行情 publication。 */
const staleEquityBarPageSchema = z
  .object({
    ...equityBarPageFields,
    availability: z.literal("SOURCE_UNAVAILABLE"),
    observedAt: dateTimeSchema,
    reasonCode: z.string().min(1).max(80),
    stale: z.literal(true),
  })
  .strict();

/** 既有日、周、月行情响应必须带完整精确覆盖谱系，未知字段一律拒绝。 */
export const equityBarPageSchema = z
  .union([availableEquityBarPageSchema, staleEquityBarPageSchema])
  .superRefine(validateEquityBarCoverageShape);

/** 拒绝把无覆盖、分页错配或空数据 publication 伪装为可用 K 线。 */
function validateEquityBarCoverageShape(
  value: {
    publicationKind: "DATA" | "ZERO_RECORD_COVERAGE";
    items: unknown[];
    nextCursor: string | null;
  },
  context: z.RefinementCtx,
): void {
  if (value.publicationKind === "ZERO_RECORD_COVERAGE") {
    if (value.items.length !== 0) {
      context.addIssue({
        code: "custom",
        path: ["items"],
        message: "ZERO_RECORD_COVERAGE 必须没有 K 线记录",
      });
    }
    if (value.nextCursor !== null) {
      context.addIssue({
        code: "custom",
        path: ["nextCursor"],
        message: "ZERO_RECORD_COVERAGE 不得继续分页",
      });
    }
    return;
  }
  if (value.items.length === 0) {
    context.addIssue({
      code: "custom",
      path: ["items"],
      message: "DATA 必须至少包含一条 K 线记录",
    });
  }
}

/** 既有公司概况响应。 */
export const equityCompanyProfileSchema = z
  .object({
    exchange: equityExchangeSchema,
    symbol: z.string().regex(/^\d{6}$/),
    dataVersion: uuidSchema,
    publishedAt: dateTimeSchema,
    identityAsOf: dateSchema,
    qualityStatus: z.literal("passed"),
    stale: z.literal(false),
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

/** 既有公司行动响应。 */
export const equityCorporateActionPageSchema = z
  .object({
    exchange: equityExchangeSchema,
    symbol: z.string().regex(/^\d{6}$/),
    dataVersion: uuidSchema,
    publishedAt: dateTimeSchema,
    qualityStatus: z.literal("passed"),
    stale: z.literal(false),
    items: z
      .array(
        z
          .object({
            actionId: uuidSchema,
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

/** 既有财务报告列表响应的页面消费字段。 */
export const equityFinancialReportPageSchema = z
  .object({
    exchange: equityExchangeSchema,
    symbol: z.string().regex(/^\d{6}$/),
    methodologyCode: z.string().min(1).max(80),
    methodologyVersion: z.number().int().positive(),
    items: z
      .array(
        z
          .object({
            reportRef: uuidSchema,
            statementType: z.enum(["BALANCE_SHEET", "INCOME_STATEMENT", "CASH_FLOW_STATEMENT"]),
            reportPeriod: dateSchema,
            periodBasis: z.enum(["POINT_IN_TIME", "YEAR_TO_DATE", "SINGLE_QUARTER", "TTM"]),
            statementScope: z.enum(["CONSOLIDATED", "PARENT", "UNKNOWN"]),
            currency: z
              .string()
              .regex(/^[A-Z]{3}$/)
              .nullable(),
            auditStatus: z.enum(["AUDITED", "UNAUDITED", "UNKNOWN"]),
            announcementDate: dateSchema.nullable(),
            methodologyCode: z.string().min(1).max(80),
            methodologyVersion: z.number().int().positive(),
            qualityStatus: z.enum(["PASSED", "WARNED"]),
          })
          .passthrough(),
      )
      .max(50),
    nextCursor: z.string().max(1024).nullable(),
    dataVersion: uuidSchema,
    publishedAt: dateTimeSchema,
    effectiveAsOf: dateSchema,
    knowledgeCutoff: dateTimeSchema,
  })
  .passthrough();

/** 既有财务报表行项目详情响应。 */
export const equityFinancialReportDetailSchema = z
  .object({
    report: z
      .object({
        reportRef: uuidSchema,
        exchange: equityExchangeSchema,
        symbol: z.string().regex(/^\d{6}$/),
        statementType: z.enum(["BALANCE_SHEET", "INCOME_STATEMENT", "CASH_FLOW_STATEMENT"]),
        reportPeriod: dateSchema,
        periodBasis: z.enum(["POINT_IN_TIME", "YEAR_TO_DATE", "SINGLE_QUARTER", "TTM"]),
        statementScope: z.enum(["CONSOLIDATED", "PARENT", "UNKNOWN"]),
        currency: z
          .string()
          .regex(/^[A-Z]{3}$/)
          .nullable(),
        auditStatus: z.enum(["AUDITED", "UNAUDITED", "UNKNOWN"]),
        methodologyCode: z.string().min(1).max(80),
        methodologyVersion: z.number().int().positive(),
        qualityStatus: z.enum(["PASSED", "WARNED"]),
      })
      .passthrough(),
    items: z
      .array(
        z
          .object({
            metricCode: z.string().min(1).max(80),
            label: z.string().min(1).max(160),
            value: decimalSchema.nullable(),
            nullReason: z.enum(["NOT_REPORTED", "NOT_APPLICABLE", "UPSTREAM_NULL"]).nullable(),
            currency: z
              .string()
              .regex(/^[A-Z]{3}$/)
              .nullable(),
            currencyNullReason: z
              .enum(["NOT_APPLICABLE", "UNKNOWN_SOURCE", "MIXED_CURRENCIES"])
              .nullable(),
            unit: z.string().min(1).max(32),
          })
          .strict(),
      )
      .max(200),
    nextCursor: z.string().max(1024).nullable(),
    dataVersion: uuidSchema,
    publishedAt: dateTimeSchema,
    effectiveAsOf: dateSchema,
    knowledgeCutoff: dateTimeSchema,
  })
  .passthrough();

/** 既有供应商直报或平台衍生财务指标响应。 */
export const equityFinancialMetricPageSchema = z
  .object({
    exchange: equityExchangeSchema,
    symbol: z.string().regex(/^\d{6}$/),
    origin: z.enum(["PROVIDER_REPORTED", "PLATFORM_DERIVED"]),
    methodologyCode: z.string().min(1).max(80),
    methodologyVersion: z.number().int().positive(),
    items: z
      .array(
        z
          .object({
            metricCode: z.string().min(1).max(80),
            label: z.string().min(1).max(160),
            origin: z.enum(["PROVIDER_REPORTED", "PLATFORM_DERIVED"]),
            reportPeriod: dateSchema,
            periodBasis: z.enum(["POINT_IN_TIME", "YEAR_TO_DATE", "SINGLE_QUARTER", "TTM"]),
            statementScope: z.enum(["CONSOLIDATED", "PARENT", "UNKNOWN"]),
            value: decimalSchema,
            unit: z.string().min(1).max(32),
            currency: z
              .string()
              .regex(/^[A-Z]{3}$/)
              .nullable(),
            methodologyCode: z.string().min(1).max(80),
            methodologyVersion: z.number().int().positive(),
            formulaVersion: z.number().int().positive().nullable(),
            revision: z.number().int().positive(),
          })
          .passthrough(),
      )
      .max(500),
    nextCursor: z.string().max(1024).nullable(),
    dataVersion: uuidSchema,
    publishedAt: dateTimeSchema,
    effectiveAsOf: dateSchema,
    knowledgeCutoff: dateTimeSchema,
  })
  .passthrough();

/** 既有历史估值响应。 */
export const equityValuationPageSchema = z
  .object({
    exchange: equityExchangeSchema,
    symbol: z.string().regex(/^\d{6}$/),
    methodologyCode: z.string().min(1).max(80),
    methodologyVersion: z.number().int().positive(),
    items: z
      .array(
        z
          .object({
            observationDate: dateSchema,
            metricCode: z.enum(["market_cap", "pe_ttm", "pe_static", "pb", "pcf"]),
            value: decimalSchema,
            unit: z.string().min(1).max(32),
            methodologyCode: z.string().min(1).max(80),
            methodologyVersion: z.number().int().positive(),
            finality: z.literal("PROVIDER_OBSERVATION"),
          })
          .passthrough(),
      )
      .max(1000),
    nextCursor: z.string().max(1024).nullable(),
    dataVersion: uuidSchema,
    publishedAt: dateTimeSchema,
    effectiveAsOf: dateSchema,
    knowledgeCutoff: dateTimeSchema,
  })
  .passthrough();

/** 既有个股日频资金流响应。 */
export const equityMoneyFlowDailyPageSchema = z
  .object({
    methodologyId: z.string().min(1).max(100),
    methodologyVersion: z.string().min(1).max(64),
    upstreamSource: z.string().min(1).max(100),
    sourceDataset: z.string().min(1).max(160),
    semanticFamily: z.enum(["trade_direction_flow", "order_size_flow"]),
    ratioDenominator: z.string().min(1).max(300),
    directionDefinition: z.string().min(1).max(500),
    currency: z
      .string()
      .regex(/^[A-Z]{3}$/)
      .nullable(),
    amountUnit: z.string().min(1).max(64),
    scope: z
      .object({
        scopeType: z.literal("equity"),
        exchange: equityExchangeSchema,
        symbol: z.string().regex(/^\d{6}$/),
        name: z.string().max(200).nullable(),
      })
      .strict(),
    universe: z.string().min(1).max(100),
    bucket: z.string().min(1).max(64),
    windowType: z.literal("daily_source"),
    windowSize: z.literal(1),
    dataVersion: uuidSchema,
    publishedAt: dateTimeSchema,
    items: z
      .array(
        z
          .object({
            tradeDate: dateSchema,
            observedAt: dateTimeSchema,
            knownFrom: dateTimeSchema,
            finality: z.enum(["source_reported_daily", "post_close_observation", "unknown"]),
            grossInflow: decimalSchema.nullable(),
            grossOutflow: decimalSchema.nullable(),
            netAmount: decimalSchema.nullable(),
            netRatio: decimalSchema.nullable(),
            qualityStatus: z.enum(["passed", "warned"]),
          })
          .strict(),
      )
      .max(500),
    nextCursor: z.string().max(2048).nullable(),
  })
  .passthrough();

/** 确保证券归属顶层 publication 与 release 上下文严格一致。 */
function hasMatchingEquitySectorDataVersion(value: {
  dataVersion: string;
  release: { dataVersion: string };
}): boolean {
  return value.dataVersion === value.release.dataVersion;
}

/** 既有证券反向行业/概念归属响应。 */
export const equitySectorPageSchema = z
  .object({
    equity: z
      .object({
        exchange: equityExchangeSchema,
        symbol: z.string().regex(/^\d{6}$/),
        name: z.string().min(1).max(200),
        listingStatus: equityListingStatusSchema,
      })
      .strict(),
    scheme: z.enum(["eastmoney.industry", "eastmoney.concept"]),
    identityAsOf: z.string().date(),
    dataVersion: uuidSchema,
    release: z
      .object({
        dataVersion: uuidSchema,
        publishedAt: dateTimeSchema,
      })
      .passthrough(),
    items: z
      .array(
        z
          .object({
            scheme: z.enum(["eastmoney.industry", "eastmoney.concept"]),
            code: z.string().min(1).max(64),
            name: z.string().min(1).max(200),
            observedFrom: dateTimeSchema,
            observedTo: dateTimeSchema.nullable(),
            snapshotObservedAt: dateTimeSchema,
            carriedForward: z.boolean(),
          })
          .strict(),
      )
      .max(500),
    nextCursor: z.string().max(1024).nullable(),
  })
  .passthrough()
  .refine(hasMatchingEquitySectorDataVersion, {
    message: "顶层与 release 的 dataVersion 必须一致",
    path: ["dataVersion"],
  });

export type EquityExchange = z.infer<typeof equityExchangeSchema>;
export type EquityListingStatus = z.infer<typeof equityListingStatusSchema>;
export type EquityTradingStatus = z.infer<typeof equityTradingStatusSchema>;
export type EquityAvailability = z.infer<typeof equityAvailabilitySchema>;
export type EquitySearchSortField = z.infer<typeof equitySearchSortFieldSchema>;
export type EquitySearchResponse = z.infer<typeof equitySearchResponseSchema>;
export type EquitySearchRecord = z.infer<typeof equitySearchRecordSchema>;
export type EquityEventFamily = z.infer<typeof equityEventFamilySchema>;
export type EquityEventPage = z.infer<typeof equityEventPageSchema>;
export type EquityDataStatusResponse = z.infer<typeof equityDataStatusResponseSchema>;
export type EquityDatasetStatus = z.infer<typeof equityDatasetStatusSchema>;
export type EquityIdentityDetail = z.infer<typeof equityIdentityDetailSchema>;
export type EquityIdentityPage = z.infer<typeof equityIdentityPageSchema>;
export type EquityListingStatusHistoryPage = z.infer<typeof equityListingStatusHistoryPageSchema>;
export type EquityBarPage = z.infer<typeof equityBarPageSchema>;
export type EquityCompanyProfile = z.infer<typeof equityCompanyProfileSchema>;
export type EquityCorporateActionPage = z.infer<typeof equityCorporateActionPageSchema>;
export type EquityFinancialReportPage = z.infer<typeof equityFinancialReportPageSchema>;
export type EquityFinancialReportDetail = z.infer<typeof equityFinancialReportDetailSchema>;
export type EquityFinancialMetricPage = z.infer<typeof equityFinancialMetricPageSchema>;
export type EquityValuationPage = z.infer<typeof equityValuationPageSchema>;
export type EquityMoneyFlowDailyPage = z.infer<typeof equityMoneyFlowDailyPageSchema>;
export type EquitySectorPage = z.infer<typeof equitySectorPageSchema>;
