import { z } from "zod";

/** 枚举前端与公开合同共同支持的四条互联互通通道。 */
export const stockConnectChannelCodes = [
  "SH_NORTHBOUND",
  "SZ_NORTHBOUND",
  "SH_SOUTHBOUND",
  "SZ_SOUTHBOUND",
] as const;

/** 表示一条带交易所路径与业务方向的互联互通通道。 */
export type StockConnectChannelCode = (typeof stockConnectChannelCodes)[number];

/** 枚举公开接口支持的活跃证券榜单口径。 */
export const stockConnectRankings = ["SOURCE_ACTIVE", "NET_BUY", "NET_SELL"] as const;

/** 表示官方活跃证券范围内的一种排序。 */
export type StockConnectRanking = (typeof stockConnectRankings)[number];

/** 枚举金额、数量与排行字段的公开可用性。 */
export const stockConnectAvailabilities = [
  "REPORTED",
  "DERIVED",
  "NOT_DISCLOSED_BY_REGIME",
  "SOURCE_MISSING",
  "NOT_APPLICABLE",
] as const;

/** 表示一个字段来自报告、派生或确定不可用的原因。 */
export type StockConnectAvailability = (typeof stockConnectAvailabilities)[number];

/** 枚举 readiness 对候选交易日逐通道给出的终态或过渡态。 */
export const stockConnectReadinessStates = [
  "READY",
  "PENDING",
  "FAILED",
  "SOURCE_MISSING",
  "NOT_TRADING",
] as const;

/** 表示由持久化证据决定的一条通道准备状态。 */
export type StockConnectReadinessState = (typeof stockConnectReadinessStates)[number];

/** 枚举可安全展示且不泄露上游异常、凭证或对象路径的 readiness 原因。 */
export const stockConnectReadinessReasonCodes = [
  "BUNDLE_PUBLISHED",
  "OFFICIAL_CALENDAR_CLOSED",
  "CALENDAR_EVIDENCE_MISSING",
  "CALENDAR_SOURCE_MISSING",
  "DELIVERY_ENTITLEMENT_MISSING",
  "DELIVERY_OBJECT_MISSING",
  "STATUS_SOURCE_MISSING",
  "PREFLIGHT_PENDING",
  "PREFLIGHT_FAILED",
  "COMMAND_NOT_SUBMITTED",
  "EXECUTION_PENDING",
  "EXECUTION_SOURCE_MISSING",
  "EXECUTION_FAILED",
  "PUBLICATION_INCOMPLETE",
] as const;

/** 表示前端可映射为固定中文说明的 readiness 原因。 */
export type StockConnectReadinessReasonCode = (typeof stockConnectReadinessReasonCodes)[number];

/** 校验 API 中不含时区推断的精确交易日。 */
const tradeDateSchema = z.iso.date();

/** 校验 API 中携带明确时区偏移的 publication 时间。 */
const publicationDateTimeSchema = z.iso.datetime({ offset: true });

/** 校验来源文件和 readiness 表示使用的小写 SHA-256。 */
const sha256Schema = z.string().regex(/^[a-f0-9]{64}$/u);

/** 冻结每种 readiness 状态允许出现的安全原因集合。 */
const readinessReasonsByState: Record<
  StockConnectReadinessState,
  ReadonlySet<StockConnectReadinessReasonCode>
> = {
  READY: new Set(["BUNDLE_PUBLISHED"]),
  NOT_TRADING: new Set(["OFFICIAL_CALENDAR_CLOSED"]),
  PENDING: new Set([
    "PREFLIGHT_PENDING",
    "COMMAND_NOT_SUBMITTED",
    "EXECUTION_PENDING",
    "PUBLICATION_INCOMPLETE",
  ]),
  FAILED: new Set(["PREFLIGHT_FAILED", "EXECUTION_FAILED", "PUBLICATION_INCOMPLETE"]),
  SOURCE_MISSING: new Set([
    "CALENDAR_EVIDENCE_MISSING",
    "CALENDAR_SOURCE_MISSING",
    "DELIVERY_ENTITLEMENT_MISSING",
    "DELIVERY_OBJECT_MISSING",
    "STATUS_SOURCE_MISSING",
    "EXECUTION_SOURCE_MISSING",
  ]),
};

/** 校验四条通道的固定代码。 */
export const stockConnectChannelCodeSchema = z.enum(stockConnectChannelCodes);

/** 校验精确日与 latest 两种互斥日期选择。 */
export const stockConnectDateSelectionSchema = z.discriminatedUnion("mode", [
  z
    .object({
      mode: z.literal("LATEST"),
      exactDate: z.null(),
    })
    .strict(),
  z
    .object({
      mode: z.literal("EXACT"),
      exactDate: tradeDateSchema,
    })
    .strict(),
]);

/** 表示公开合同接受的日期选择，不把抓取时间当成交易日。 */
export type StockConnectDateSelection = z.infer<typeof stockConnectDateSelectionSchema>;

/** 检查通道数组不含重复值。 */
function hasUniqueChannels(values: readonly StockConnectChannelCode[]): boolean {
  return new Set(values).size === values.length;
}

/** 检查不透明版本标识不含 ASCII 控制字符，同时允许未来采用非 UUID 版本格式。 */
function isSafeStockConnectDataVersion(value: string): boolean {
  for (const character of value) {
    const codePoint = character.codePointAt(0);
    if (codePoint !== undefined && (codePoint <= 0x1f || codePoint === 0x7f)) return false;
  }
  return true;
}

/** 校验总览查询请求。 */
export const stockConnectOverviewQuerySchema = z
  .object({
    date: stockConnectDateSelectionSchema,
    channels: z
      .array(stockConnectChannelCodeSchema)
      .min(1)
      .max(4)
      .refine(hasUniqueChannels, "通道不能重复。"),
    trendTradingDays: z.number().int().min(1).max(250),
  })
  .strict();

/** 表示总览 POST 请求体。 */
export type StockConnectOverviewQuery = z.infer<typeof stockConnectOverviewQuerySchema>;

/** 校验独立 readiness 查询，避免同步状态改变业务 publication 的不可变版本。 */
export const stockConnectReadinessQuerySchema = z
  .object({
    date: stockConnectDateSelectionSchema,
    channels: z
      .array(stockConnectChannelCodeSchema)
      .min(1)
      .max(4)
      .refine(hasUniqueChannels, "通道不能重复。"),
  })
  .strict();

/** 表示候选交易日与逐通道准备状态查询。 */
export type StockConnectReadinessQuery = z.infer<typeof stockConnectReadinessQuerySchema>;

/** 校验单通道详情查询请求。 */
export const stockConnectChannelQuerySchema = z
  .object({
    date: stockConnectDateSelectionSchema,
    channel: stockConnectChannelCodeSchema,
    trendTradingDays: z.number().int().min(1).max(250),
  })
  .strict();

/** 表示单通道 POST 请求体。 */
export type StockConnectChannelQuery = z.infer<typeof stockConnectChannelQuerySchema>;

/** 校验官方活跃证券榜查询请求。 */
export const stockConnectActiveSecurityQuerySchema = z
  .object({
    date: stockConnectDateSelectionSchema,
    channel: stockConnectChannelCodeSchema,
    ranking: z.enum(stockConnectRankings),
    parentPublicationDataVersion: z
      .string()
      .min(1)
      .max(160)
      .refine(isSafeStockConnectDataVersion, "父 publication 版本不能包含控制字符。"),
    cursor: z.string().max(1024).nullable(),
    limit: z.number().int().min(1).max(100),
  })
  .strict();

/** 表示官方活跃证券榜 POST 请求体。 */
export type StockConnectActiveSecurityQuery = z.infer<typeof stockConnectActiveSecurityQuerySchema>;

/** 校验证券互联互通上下文查询请求。 */
export const stockConnectSecurityContextQuerySchema = z
  .object({
    instrumentEntityRef: z.string().min(1).max(160),
    date: stockConnectDateSelectionSchema,
    channel: stockConnectChannelCodeSchema.nullable(),
    historyTradingDays: z.number().int().min(1).max(250),
  })
  .strict();

/** 表示证券上下文 POST 请求体。 */
export type StockConnectSecurityContextQuery = z.infer<
  typeof stockConnectSecurityContextQuerySchema
>;

/** 校验原币基础单位金额，业务层不得转换币种或隐式缩放。 */
export const stockConnectMoneySchema = z
  .object({
    amount: z
      .string()
      .min(1)
      .max(80)
      .regex(/^-?[0-9]+(?:\.[0-9]+)?$/u),
    currency: z.enum(["CNY", "HKD"]),
    unit: z.literal("BASE"),
  })
  .strict();

/** 表示保留十进制字符串精度的原币金额。 */
export type StockConnectMoney = z.infer<typeof stockConnectMoneySchema>;

/** 校验买入、卖出、成交额和额度使用的非负原币金额。 */
const nonNegativeMoneySchema = stockConnectMoneySchema.extend({
  amount: z
    .string()
    .min(1)
    .max(80)
    .regex(/^[0-9]+(?:\.[0-9]+)?$/u),
});

/** 构造具有真实值和 lineage 的已披露或派生金额。 */
function availableMoneyFactSchema(
  availability: "REPORTED" | "DERIVED",
  valueSchema: z.ZodType<StockConnectMoney> = stockConnectMoneySchema,
) {
  return z
    .object({
      availability: z.literal(availability),
      value: valueSchema,
      lineageRef: z.string().min(1).max(200),
    })
    .strict();
}

/** 构造因制度、来源或适用性而没有值的金额。 */
function unavailableMoneyFactSchema(
  availability: "NOT_DISCLOSED_BY_REGIME" | "SOURCE_MISSING" | "NOT_APPLICABLE",
) {
  return z
    .object({
      availability: z.literal(availability),
      value: z.null(),
      lineageRef: z.string().max(200).nullable(),
    })
    .strict();
}

/** 校验金额事实，禁止用零替代未披露或来源缺失。 */
export const stockConnectMoneyFactSchema = z.discriminatedUnion("availability", [
  availableMoneyFactSchema("REPORTED"),
  availableMoneyFactSchema("DERIVED"),
  unavailableMoneyFactSchema("NOT_DISCLOSED_BY_REGIME"),
  unavailableMoneyFactSchema("SOURCE_MISSING"),
  unavailableMoneyFactSchema("NOT_APPLICABLE"),
]);

/** 表示一个带 publication 可用性与 lineage 的金额事实。 */
export type StockConnectMoneyFact = z.infer<typeof stockConnectMoneyFactSchema>;

/** 校验只能由来源报告的非负金额，防止把派生值或负数当作成交字段。 */
const reportedNonNegativeMoneyFactSchema = z.discriminatedUnion("availability", [
  availableMoneyFactSchema("REPORTED", nonNegativeMoneySchema),
  unavailableMoneyFactSchema("NOT_DISCLOSED_BY_REGIME"),
  unavailableMoneyFactSchema("SOURCE_MISSING"),
  unavailableMoneyFactSchema("NOT_APPLICABLE"),
]);

/** 校验只能由同源买卖字段派生的有符号净额。 */
const derivedSignedMoneyFactSchema = z.discriminatedUnion("availability", [
  availableMoneyFactSchema("DERIVED"),
  unavailableMoneyFactSchema("NOT_DISCLOSED_BY_REGIME"),
  unavailableMoneyFactSchema("SOURCE_MISSING"),
  unavailableMoneyFactSchema("NOT_APPLICABLE"),
]);

/** 描述需要满足买卖、成交额和净额恒等式的一组金额事实。 */
type MoneyIdentityFacts = {
  buyAmount: z.infer<typeof reportedNonNegativeMoneyFactSchema>;
  sellAmount: z.infer<typeof reportedNonNegativeMoneyFactSchema>;
  turnoverAmount: z.infer<typeof reportedNonNegativeMoneyFactSchema>;
  netBuyAmount: z.infer<typeof derivedSignedMoneyFactSchema>;
};

/** 校验同一行金额的来源类型、币种以及 buy±sell 恒等式，不使用浮点数。 */
function validateMoneyIdentity(facts: MoneyIdentityFacts, context: z.RefinementCtx): void {
  const buy = facts.buyAmount.availability === "REPORTED" ? facts.buyAmount.value : null;
  const sell = facts.sellAmount.availability === "REPORTED" ? facts.sellAmount.value : null;
  const turnover =
    facts.turnoverAmount.availability === "REPORTED" ? facts.turnoverAmount.value : null;
  const net = facts.netBuyAmount.availability === "DERIVED" ? facts.netBuyAmount.value : null;

  if (buy !== null && sell !== null) {
    if (
      turnover === null ||
      !sameCurrency(buy, sell, turnover) ||
      !decimalIdentityHolds(buy.amount, sell.amount, turnover.amount, "ADD")
    ) {
      context.addIssue({
        code: "custom",
        path: ["turnoverAmount"],
        message: "reported turnover must equal buy plus sell in one currency",
      });
    }
    if (
      net === null ||
      !sameCurrency(buy, sell, net) ||
      !decimalIdentityHolds(buy.amount, sell.amount, net.amount, "SUBTRACT")
    ) {
      context.addIssue({
        code: "custom",
        path: ["netBuyAmount"],
        message: "derived net amount must equal buy minus sell in one currency",
      });
    }
    return;
  }

  if (net !== null) {
    context.addIssue({
      code: "custom",
      path: ["netBuyAmount"],
      message: "derived net amount requires reported buy and sell inputs",
    });
  }
}

/** 判断三个金额是否具有相同原币和基础单位。 */
function sameCurrency(
  left: StockConnectMoney,
  right: StockConnectMoney,
  result: StockConnectMoney,
): boolean {
  return (
    left.currency === right.currency &&
    right.currency === result.currency &&
    left.unit === right.unit &&
    right.unit === result.unit
  );
}

/** 使用按最大小数位对齐的 BigInt 验证加减恒等式，避免大额浮点精度丢失。 */
function decimalIdentityHolds(
  left: string,
  right: string,
  result: string,
  operation: "ADD" | "SUBTRACT",
): boolean {
  const scale = Math.max(decimalScale(left), decimalScale(right), decimalScale(result));
  const leftInteger = scaledDecimalInteger(left, scale);
  const rightInteger = scaledDecimalInteger(right, scale);
  const resultInteger = scaledDecimalInteger(result, scale);
  return operation === "ADD"
    ? leftInteger + rightInteger === resultInteger
    : leftInteger - rightInteger === resultInteger;
}

/** 返回十进制字符串的小数位数。 */
function decimalScale(value: string): number {
  return value.split(".", 2)[1]?.length ?? 0;
}

/** 把已通过有界正则校验的十进制字符串转换为指定小数位的整数。 */
function scaledDecimalInteger(value: string, scale: number): bigint {
  const negative = value.startsWith("-");
  const unsigned = negative ? value.slice(1) : value;
  const [whole = "0", fraction = ""] = unsigned.split(".", 2);
  const digits = BigInt(`${whole}${fraction}`);
  const aligned = digits * 10n ** BigInt(scale - fraction.length);
  return negative ? -aligned : aligned;
}

/** 校验具有真实值和 lineage 的已披露计数。 */
const availableCountFactSchema = z
  .object({
    availability: z.enum(["REPORTED", "DERIVED"]),
    value: z.number().int().min(0),
    lineageRef: z.string().min(1).max(200),
  })
  .strict();

/** 校验因制度、来源或适用性而没有值的计数。 */
const unavailableCountFactSchema = z
  .object({
    availability: z.enum(["NOT_DISCLOSED_BY_REGIME", "SOURCE_MISSING", "NOT_APPLICABLE"]),
    value: z.null(),
    lineageRef: z.string().max(200).nullable(),
  })
  .strict();

/** 校验计数事实，禁止未披露时填入零。 */
const stockConnectCountFactSchema = z.union([availableCountFactSchema, unavailableCountFactSchema]);

/** 校验通道市场级统计，成交额与资金净额保持独立字段。 */
const stockConnectMarketStatsSchema = z
  .object({
    buyAmount: reportedNonNegativeMoneyFactSchema,
    sellAmount: reportedNonNegativeMoneyFactSchema,
    turnoverAmount: reportedNonNegativeMoneyFactSchema,
    netBuyAmount: derivedSignedMoneyFactSchema,
    tradeCount: stockConnectCountFactSchema,
    etfTurnoverAmount: reportedNonNegativeMoneyFactSchema,
  })
  .strict()
  .superRefine(validateMoneyIdentity);

/** 表示一个通道在一个交易日的完整市场统计字段集。 */
export type StockConnectMarketStats = z.infer<typeof stockConnectMarketStatsSchema>;

/** 校验额度以外的日终通道状态公共字段。 */
const stockConnectChannelStatusBaseSchema = z
  .object({
    tradingDay: z.boolean(),
    sessionState: z.enum(["OPEN", "CLOSED", "HALTED", "NOT_OPEN", "UNKNOWN"]),
    buyOrderAccepted: z.boolean().nullable(),
    sellOrderAccepted: z.boolean().nullable(),
    observedAt: publicationDateTimeSchema,
    finality: z.literal("END_OF_DAY"),
  })
  .strict();

/** 校验额度充足但制度不公开阈值以上具体余额的状态。 */
const sufficientQuotaStatusSchema = stockConnectChannelStatusBaseSchema
  .extend({
    quotaState: z.literal("SUFFICIENT"),
    quotaBalance: unavailableMoneyFactSchema("NOT_DISCLOSED_BY_REGIME"),
  })
  .strict();

/** 校验以 CNY 报告具体余额或额度用尽的状态。 */
const reportedQuotaStatusSchema = stockConnectChannelStatusBaseSchema
  .extend({
    quotaState: z.enum(["ACTUAL_REPORTED", "EXHAUSTED"]),
    quotaBalance: availableMoneyFactSchema(
      "REPORTED",
      nonNegativeMoneySchema.extend({ currency: z.literal("CNY") }),
    ),
  })
  .strict();

/** 校验日终额度来源缺失的状态。 */
const missingQuotaStatusSchema = stockConnectChannelStatusBaseSchema
  .extend({
    quotaState: z.literal("SOURCE_MISSING"),
    quotaBalance: unavailableMoneyFactSchema("SOURCE_MISSING"),
  })
  .strict();

/** 校验通道不适用额度字段的状态。 */
const notApplicableQuotaStatusSchema = stockConnectChannelStatusBaseSchema
  .extend({
    quotaState: z.literal("NOT_APPLICABLE"),
    quotaBalance: unavailableMoneyFactSchema("NOT_APPLICABLE"),
  })
  .strict();

/** 校验只允许日终展示且额度语义自洽的通道状态。 */
const stockConnectChannelStatusSchema = z.union([
  sufficientQuotaStatusSchema,
  reportedQuotaStatusSchema,
  missingQuotaStatusSchema,
  notApplicableQuotaStatusSchema,
]);

/** 表示通道日终交易、买卖接受与额度状态。 */
export type StockConnectChannelStatus = z.infer<typeof stockConnectChannelStatusSchema>;

/** 定义来源引用在两种 publication 可用性分支共享的追溯字段。 */
const stockConnectSourceRefBaseSchema = z.object({
  sourceCode: z.enum([
    "HKEX_DATA_MARKETPLACE",
    "HKEX_OMDC",
    "HKEX_CALENDAR",
    "SSE_MDGW",
    "SZSE_STEP",
  ]),
  productName: z.string().min(1).max(160),
  sourceObservedAt: publicationDateTimeSchema,
  sourceFileSha256: z
    .string()
    .regex(/^[a-f0-9]{64}$/u)
    .nullable(),
});

/** 校验来源真实发布时间与“来源未提供”状态严格判别，接收时间不能冒充发布时间。 */
const stockConnectSourceRefSchema = z.discriminatedUnion("sourcePublicationAvailability", [
  stockConnectSourceRefBaseSchema
    .extend({
      sourcePublicationAvailability: z.literal("REPORTED"),
      sourcePublicationAt: publicationDateTimeSchema,
    })
    .strict(),
  stockConnectSourceRefBaseSchema
    .extend({
      sourcePublicationAvailability: z.literal("NOT_PROVIDED_BY_SOURCE"),
      sourcePublicationAt: z.null(),
    })
    .strict(),
]);

/** 校验 publication 中可公开的质量问题。 */
const stockConnectQualityIssueSchema = z
  .object({
    code: z.enum([
      "IDENTITY_SOURCE_UNRESOLVED",
      "STATUS_SOURCE_NOT_AVAILABLE_HISTORICAL",
      "SESSION_STATE_DERIVED_FROM_CALENDAR_AND_FINALITY",
      "OPTIONAL_FIELD_SOURCE_MISSING",
    ]),
    component: z.string().min(1).max(120),
    detail: z.string().min(1).max(300),
  })
  .strict();

/** 校验一次不可变 bundle publication 的来源与质量元数据。 */
export const stockConnectPublicationSchema = z
  .object({
    bundleReleaseId: z.string().uuid(),
    dataVersion: z.string().min(1).max(160),
    tradeDate: tradeDateSchema,
    publishedAt: publicationDateTimeSchema,
    qualityStatus: z.enum(["APPROVED", "APPROVED_WITH_WARNINGS"]),
    qualityIssues: z.array(stockConnectQualityIssueSchema).max(20),
    sourceRefs: z.array(stockConnectSourceRefSchema).min(1).max(12),
  })
  .strict();

/** 表示页面显示和条件复核使用的真实 publication。 */
export type StockConnectPublication = z.infer<typeof stockConnectPublicationSchema>;

/** 校验 readiness 的官方日历证据及其独立 publication 时间可用性。 */
const stockConnectReadinessCalendarSchema = z
  .object({
    dataVersion: sha256Schema,
    observedAt: publicationDateTimeSchema.nullable(),
    sourceFileSha256: sha256Schema.nullable(),
    sourcePublicationAt: publicationDateTimeSchema.nullable(),
    publicationAvailability: z.enum(["REPORTED", "NOT_REPORTED", "SOURCE_MISSING"]),
  })
  .strict()
  .superRefine((calendar, context) => {
    const hasReportedPublication = calendar.publicationAvailability === "REPORTED";
    if (hasReportedPublication !== (calendar.sourcePublicationAt !== null)) {
      context.addIssue({
        code: "custom",
        path: ["sourcePublicationAt"],
        message: "日历 publication 可用性与时间不一致。",
      });
    }
  });

/** 校验候选交易日上一条通道的持久化 calendar、run 与 publication 证据。 */
export const stockConnectReadinessChannelSchema = z
  .object({
    channel: stockConnectChannelCodeSchema,
    calendarState: z.enum(["OPEN", "CLOSED", "UNKNOWN"]),
    state: z.enum(stockConnectReadinessStates),
    reasonCode: z.enum(stockConnectReadinessReasonCodes),
    bundleDataVersion: z.string().min(1).max(160).nullable(),
    evidenceObservedAt: publicationDateTimeSchema,
  })
  .strict()
  .superRefine((item, context) => {
    if (!readinessReasonsByState[item.state].has(item.reasonCode)) {
      context.addIssue({
        code: "custom",
        path: ["reasonCode"],
        message: "readiness 原因与状态不一致。",
      });
    }
    if ((item.state === "READY") !== (item.bundleDataVersion !== null)) {
      context.addIssue({
        code: "custom",
        path: ["bundleDataVersion"],
        message: "只有 READY 通道可携带 bundle 版本。",
      });
    }
    if (item.state === "READY" && item.calendarState !== "OPEN") {
      context.addIssue({
        code: "custom",
        path: ["calendarState"],
        message: "READY 通道必须具有官方开放日证据。",
      });
    }
    if (
      item.state === "NOT_TRADING" &&
      (item.calendarState !== "CLOSED" || item.reasonCode !== "OFFICIAL_CALENDAR_CLOSED")
    ) {
      context.addIssue({
        code: "custom",
        path: ["calendarState"],
        message: "NOT_TRADING 必须由官方闭市日证据支持。",
      });
    }
  });

/** 表示一条通道在候选交易日的机器可判定 readiness。 */
export type StockConnectReadinessChannel = z.infer<typeof stockConnectReadinessChannelSchema>;

/** 校验 readiness 的日期模式、稳定通道矩阵、证据最大时间和 ready 日期语义。 */
function validateStockConnectReadiness(
  response: {
    mode: "LATEST" | "EXACT";
    selectedChannels: StockConnectChannelCode[];
    requestedExactDate: string | null;
    candidateTradeDate: string | null;
    readyTradeDate: string | null;
    observedAt: string;
    calendar: z.infer<typeof stockConnectReadinessCalendarSchema>;
    channels: StockConnectReadinessChannel[];
  },
  context: z.RefinementCtx,
): void {
  const sortedChannels = [...response.selectedChannels].sort();
  if (
    sortedChannels.some((channel, index) => channel !== response.selectedChannels[index]) ||
    response.channels.length !== response.selectedChannels.length ||
    response.channels.some((item, index) => item.channel !== response.selectedChannels[index])
  ) {
    context.addIssue({
      code: "custom",
      path: ["channels"],
      message: "readiness 通道必须与稳定排序后的选择完全一致。",
    });
  }

  const allReady = response.channels.every((item) => item.state === "READY");
  if (response.mode === "EXACT") {
    if (
      response.requestedExactDate === null ||
      (response.candidateTradeDate !== null &&
        response.candidateTradeDate !== response.requestedExactDate) ||
      (allReady && response.candidateTradeDate !== response.requestedExactDate) ||
      response.readyTradeDate !== (allReady ? response.requestedExactDate : null)
    ) {
      context.addIssue({
        code: "custom",
        path: ["requestedExactDate"],
        message: "精确日 readiness 日期不一致。",
      });
    }
  } else if (
    response.requestedExactDate !== null ||
    (response.candidateTradeDate !== null &&
      response.readyTradeDate !== null &&
      response.readyTradeDate > response.candidateTradeDate) ||
    (allReady && response.readyTradeDate !== response.candidateTradeDate)
  ) {
    context.addIssue({
      code: "custom",
      path: ["readyTradeDate"],
      message: "latest readiness 日期不一致。",
    });
  }

  const evidenceTimes = [
    response.calendar.observedAt,
    response.calendar.sourcePublicationAt,
    ...response.channels.map((item) => item.evidenceObservedAt),
  ].filter((value): value is string => value !== null);
  const observedAt = Date.parse(response.observedAt);
  const latestEvidenceAt = Math.max(...evidenceTimes.map((value) => Date.parse(value)));
  if (observedAt !== latestEvidenceAt) {
    context.addIssue({
      code: "custom",
      path: ["observedAt"],
      message: "readiness 观察时间必须等于最近持久化证据时间。",
    });
  }
}

/** 校验独立版本化 readiness 快照，禁止浏览器根据当前时间自行猜测状态。 */
export const stockConnectReadinessResponseSchema = z
  .object({
    schemaVersion: z.literal("quant-v2.stock-connect-readiness.v1"),
    dataVersion: sha256Schema,
    mode: z.enum(["LATEST", "EXACT"]),
    selectedChannels: z
      .array(stockConnectChannelCodeSchema)
      .min(1)
      .max(4)
      .refine(hasUniqueChannels, "readiness 通道不能重复。"),
    requestedExactDate: tradeDateSchema.nullable(),
    candidateTradeDate: tradeDateSchema.nullable(),
    readyTradeDate: tradeDateSchema.nullable(),
    observedAt: publicationDateTimeSchema,
    calendar: stockConnectReadinessCalendarSchema,
    channels: z.array(stockConnectReadinessChannelSchema).min(1).max(4),
  })
  .strict()
  .superRefine(validateStockConnectReadiness);

/** 表示不污染业务 publication 的候选交易日与逐通道准备状态。 */
export type StockConnectReadinessResponse = z.infer<typeof stockConnectReadinessResponseSchema>;

/** 校验一个通道在解析交易日的事实摘要。 */
export const stockConnectChannelSummarySchema = z
  .object({
    channel: stockConnectChannelCodeSchema,
    direction: z.enum(["NORTHBOUND", "SOUTHBOUND"]),
    route: z.enum(["SHANGHAI", "SHENZHEN"]),
    tradeDate: tradeDateSchema,
    stats: stockConnectMarketStatsSchema,
    status: stockConnectChannelStatusSchema,
    activeSecurityCount: z.number().int().min(0).max(10),
  })
  .strict();

/** 表示一条通道的日终统计、状态和来源榜规模。 */
export type StockConnectChannelSummary = z.infer<typeof stockConnectChannelSummarySchema>;

/** 校验带通道代码的日终趋势点，避免不同通道序列失去身份。 */
export const stockConnectTrendPointSchema = z
  .object({
    channel: stockConnectChannelCodeSchema,
    tradeDate: tradeDateSchema,
    dataVersion: z.string().min(1).max(160),
    stats: stockConnectMarketStatsSchema,
    status: stockConnectChannelStatusSchema,
  })
  .strict();

/** 表示一条明确通道的日终趋势点。 */
export type StockConnectTrendPoint = z.infer<typeof stockConnectTrendPointSchema>;

/** 校验四通道共同 publication 总览。 */
export const stockConnectOverviewResponseSchema = z
  .object({
    resolvedTradeDate: tradeDateSchema,
    dateResolution: z.enum(["LATEST_COMMON", "EXACT"]),
    channels: z.array(stockConnectChannelSummarySchema).min(1).max(4),
    trend: z.array(stockConnectTrendPointSchema).max(1000),
    publication: stockConnectPublicationSchema,
  })
  .strict();

/** 表示互联互通总览成功响应。 */
export type StockConnectOverviewResponse = z.infer<typeof stockConnectOverviewResponseSchema>;

/** 校验单通道详情及其历史趋势。 */
export const stockConnectChannelResponseSchema = z
  .object({
    resolvedTradeDate: tradeDateSchema,
    dateResolution: z.enum(["LATEST_CHANNEL", "EXACT"]),
    channel: stockConnectChannelSummarySchema,
    trend: z.array(stockConnectTrendPointSchema).max(250),
    publication: stockConnectPublicationSchema,
  })
  .strict();

/** 表示单通道详情成功响应。 */
export type StockConnectChannelResponse = z.infer<typeof stockConnectChannelResponseSchema>;

/** 校验已解析且拥有稳定 entityRef 的证券身份。 */
const resolvedInstrumentIdentitySchema = z
  .object({
    identityAvailability: z.literal("RESOLVED"),
    instrumentEntityRef: z.string().min(1).max(160),
    sourceSecurityCode: z.string().min(1).max(32),
    displayName: z.string().max(160).nullable(),
    listingVenue: z.enum(["SSE", "SZSE", "HKEX"]),
  })
  .strict();

/** 校验来源身份未解析且绝不生成 entityRef 的降级投影。 */
const unresolvedInstrumentIdentitySchema = z
  .object({
    identityAvailability: z.literal("SOURCE_UNRESOLVED"),
    instrumentEntityRef: z.null(),
    sourceSecurityCode: z.string().min(1).max(32),
    displayName: z.string().max(160).nullable(),
    listingVenue: z.enum(["SSE", "SZSE", "HKEX"]),
  })
  .strict();

/** 校验稳定证券身份或来源未解析身份的强互斥投影。 */
export const stockConnectInstrumentIdentitySchema = z.discriminatedUnion("identityAvailability", [
  resolvedInstrumentIdentitySchema,
  unresolvedInstrumentIdentitySchema,
]);

/** 表示只为互联互通事实所需的最小证券身份。 */
export type StockConnectInstrumentIdentity = z.infer<typeof stockConnectInstrumentIdentitySchema>;

/** 校验来源活跃证券榜中的一条真实记录。 */
export const stockConnectActiveSecuritySchema = z
  .object({
    rankingRank: z.number().int().min(1).max(10),
    sourceRank: z.number().int().min(1).max(10),
    identity: stockConnectInstrumentIdentitySchema,
    buyAmount: reportedNonNegativeMoneyFactSchema,
    sellAmount: reportedNonNegativeMoneyFactSchema,
    turnoverAmount: reportedNonNegativeMoneyFactSchema,
    netBuyAmount: derivedSignedMoneyFactSchema,
  })
  .strict()
  .superRefine(validateMoneyIdentity);

/** 表示官方活跃证券范围内的一条记录。 */
export type StockConnectActiveSecurity = z.infer<typeof stockConnectActiveSecuritySchema>;

/** 校验官方活跃证券榜及其榜内排序可用性。 */
export const stockConnectActiveSecurityPageSchema = z
  .object({
    resolvedTradeDate: tradeDateSchema,
    dateResolution: z.enum(["LATEST_COMMON", "LATEST_CHANNEL", "EXACT"]),
    channel: stockConnectChannelCodeSchema,
    ranking: z.enum(stockConnectRankings),
    rankingAvailability: z.enum(stockConnectAvailabilities),
    rankingScope: z.literal("SOURCE_ACTIVE_SECURITIES_ONLY"),
    items: z.array(stockConnectActiveSecuritySchema).max(100),
    nextCursor: z.string().max(1024).nullable(),
    publication: stockConnectPublicationSchema,
  })
  .strict();

/** 表示带游标的来源活跃证券榜成功响应。 */
export type StockConnectActiveSecurityPage = z.infer<typeof stockConnectActiveSecurityPageSchema>;

/** 校验证券在一条通道、一个交易日的来源活跃榜事实。 */
export const stockConnectSecurityChannelActivitySchema = z
  .object({
    channel: stockConnectChannelCodeSchema,
    tradeDate: tradeDateSchema,
    dataVersion: z.string().min(1).max(160),
    sourceRank: z.number().int().min(1).max(10),
    turnoverAmount: reportedNonNegativeMoneyFactSchema,
    netBuyAmount: derivedSignedMoneyFactSchema,
  })
  .strict();

/** 表示证券在互联互通来源活跃榜内的一次出现。 */
export type StockConnectSecurityChannelActivity = z.infer<
  typeof stockConnectSecurityChannelActivitySchema
>;

/** 校验证券身份及其互联互通历史上下文。 */
export const stockConnectSecurityContextResponseSchema = z
  .object({
    resolvedTradeDate: tradeDateSchema,
    identity: stockConnectInstrumentIdentitySchema,
    activities: z.array(stockConnectSecurityChannelActivitySchema).max(1000),
    publication: stockConnectPublicationSchema,
  })
  .strict();

/** 表示证券互联互通上下文成功响应。 */
export type StockConnectSecurityContextResponse = z.infer<
  typeof stockConnectSecurityContextResponseSchema
>;

/** 将响应实体与不可变 publication 版本绑定，供 TanStack Query 条件复核。 */
export interface VersionedStockConnectResponse<T> {
  data: T;
  dataVersion: string;
  etag: string;
}
