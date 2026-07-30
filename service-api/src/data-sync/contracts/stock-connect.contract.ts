import { z } from 'zod';

/** 约束互联互通四条独立业务通道，禁止方向或交易所被隐式合并。 */
export const STOCK_CONNECT_CHANNELS = [
  'SH_NORTHBOUND',
  'SZ_NORTHBOUND',
  'SH_SOUTHBOUND',
  'SZ_SOUTHBOUND',
] as const;

/** 约束活跃证券接口允许的来源榜与榜内派生净额排序。 */
export const STOCK_CONNECT_RANKINGS = ['SOURCE_ACTIVE', 'NET_BUY', 'NET_SELL'] as const;

/** 表示内部和公开合同允许的字段级数据可用性。 */
export const STOCK_CONNECT_AVAILABILITIES = [
  'REPORTED',
  'DERIVED',
  'NOT_DISCLOSED_BY_REGIME',
  'SOURCE_MISSING',
  'NOT_APPLICABLE',
] as const;

const dateSchema = z.string().date();
const dateTimeSchema = z.string().datetime({ offset: true });
const decimalStringSchema = z
  .string()
  .min(1)
  .max(80)
  .regex(/^-?[0-9]+(?:\.[0-9]+)?$/);
const nonNegativeDecimalStringSchema = z
  .string()
  .min(1)
  .max(80)
  .regex(/^[0-9]+(?:\.[0-9]+)?$/);
const sha256Schema = z.string().regex(/^[a-f0-9]{64}$/);
/** 接受 UUID 之外的版本标识，但拒绝无法安全进入日志、Header 或游标的控制字符。 */
const publicationDataVersionSchema = z
  .string()
  .min(1)
  .max(160)
  .refine(isSafeStockConnectDataVersion, 'publication version contains control characters');
const channelSchema = z.enum(STOCK_CONNECT_CHANNELS);
const availabilitySchema = z.enum(STOCK_CONNECT_AVAILABILITIES);

/** 枚举持久化 readiness 对候选交易日逐通道给出的终态或过渡态。 */
const STOCK_CONNECT_READINESS_STATES = [
  'READY',
  'PENDING',
  'FAILED',
  'SOURCE_MISSING',
  'NOT_TRADING',
] as const;

/** 枚举 readiness 快照可公开且不泄露上游路径或异常原文的稳定原因。 */
const STOCK_CONNECT_READINESS_REASON_CODES = [
  'BUNDLE_PUBLISHED',
  'OFFICIAL_CALENDAR_CLOSED',
  'CALENDAR_EVIDENCE_MISSING',
  'CALENDAR_SOURCE_MISSING',
  'DELIVERY_ENTITLEMENT_MISSING',
  'DELIVERY_OBJECT_MISSING',
  'STATUS_SOURCE_MISSING',
  'PREFLIGHT_PENDING',
  'PREFLIGHT_FAILED',
  'COMMAND_NOT_SUBMITTED',
  'EXECUTION_PENDING',
  'EXECUTION_SOURCE_MISSING',
  'EXECUTION_FAILED',
  'PUBLICATION_INCOMPLETE',
] as const;

type StockConnectReadinessState = (typeof STOCK_CONNECT_READINESS_STATES)[number];
type StockConnectReadinessReasonCode = (typeof STOCK_CONNECT_READINESS_REASON_CODES)[number];

/** 检查不透明版本标识不含 ASCII 控制字符，同时保留未来非 UUID 格式的扩展空间。 */
export function isSafeStockConnectDataVersion(value: string): boolean {
  for (const character of value) {
    const codePoint = character.codePointAt(0);
    if (codePoint !== undefined && (codePoint <= 0x1f || codePoint === 0x7f)) return false;
  }
  return true;
}

/** 冻结每种 readiness 状态允许出现的安全原因集合。 */
const READINESS_REASONS_BY_STATE: Record<
  StockConnectReadinessState,
  ReadonlySet<StockConnectReadinessReasonCode>
> = {
  READY: new Set(['BUNDLE_PUBLISHED']),
  NOT_TRADING: new Set(['OFFICIAL_CALENDAR_CLOSED']),
  PENDING: new Set([
    'PREFLIGHT_PENDING',
    'COMMAND_NOT_SUBMITTED',
    'EXECUTION_PENDING',
    'PUBLICATION_INCOMPLETE',
  ]),
  FAILED: new Set(['PREFLIGHT_FAILED', 'EXECUTION_FAILED', 'PUBLICATION_INCOMPLETE']),
  SOURCE_MISSING: new Set([
    'CALENDAR_EVIDENCE_MISSING',
    'CALENDAR_SOURCE_MISSING',
    'DELIVERY_ENTITLEMENT_MISSING',
    'DELIVERY_OBJECT_MISSING',
    'STATUS_SOURCE_MISSING',
    'EXECUTION_SOURCE_MISSING',
  ]),
};

/** 校验 latest 与 exact 两种互斥日期选择，禁止 exact 查询静默回退。 */
export const stockConnectDateSelectionSchema = z.discriminatedUnion('mode', [
  z.object({ mode: z.literal('LATEST'), exactDate: z.null() }).strict(),
  z.object({ mode: z.literal('EXACT'), exactDate: dateSchema }).strict(),
]);

/** 判断通道数组没有重复成员，避免同一事实被重复展示或汇总。 */
function hasUniqueChannels(channels: readonly string[]): boolean {
  return new Set(channels).size === channels.length;
}

/** 校验互联互通总览内部请求。 */
export const stockConnectOverviewQuerySchema = z
  .object({
    date: stockConnectDateSelectionSchema,
    channels: z
      .array(channelSchema)
      .min(1)
      .max(4)
      .refine(hasUniqueChannels, 'channels must be unique'),
    trendTradingDays: z.number().int().min(1).max(250),
  })
  .strict();

/** 校验独立 readiness 查询，避免可变同步状态污染不可变业务 publication。 */
export const stockConnectReadinessQuerySchema = z
  .object({
    date: stockConnectDateSelectionSchema,
    channels: z
      .array(channelSchema)
      .min(1)
      .max(4)
      .refine(hasUniqueChannels, 'channels must be unique'),
  })
  .strict();

/** 校验单通道详情内部请求。 */
export const stockConnectChannelQuerySchema = z
  .object({
    date: stockConnectDateSelectionSchema,
    channel: channelSchema,
    trendTradingDays: z.number().int().min(1).max(250),
  })
  .strict();

/** 校验官方活跃证券榜及榜内净额排序内部请求。 */
export const stockConnectActiveSecurityQuerySchema = z
  .object({
    date: stockConnectDateSelectionSchema,
    channel: channelSchema,
    ranking: z.enum(STOCK_CONNECT_RANKINGS),
    parentPublicationDataVersion: publicationDataVersionSchema,
    cursor: z.string().max(1024).nullable(),
    limit: z.number().int().min(1).max(100),
  })
  .strict();

/** 校验证券在互联互通范围内的身份与历史上下文内部请求。 */
export const stockConnectSecurityContextQuerySchema = z
  .object({
    instrumentEntityRef: z.string().min(1).max(160),
    date: stockConnectDateSelectionSchema,
    channel: channelSchema.nullable(),
    historyTradingDays: z.number().int().min(1).max(250),
  })
  .strict();

/** 校验以基础货币单位表达且不丢失精度的金额。 */
const moneySchema = z
  .object({
    amount: decimalStringSchema,
    currency: z.enum(['CNY', 'HKD']),
    unit: z.literal('BASE'),
  })
  .strict();

/** 校验买入、卖出、成交额和额度使用的非负原币金额。 */
const nonNegativeMoneySchema = moneySchema.extend({
  amount: nonNegativeDecimalStringSchema,
});

/** 构造官方报告或平台派生的非空金额事实 schema。 */
function availableMoneyFactSchema(
  availability: 'REPORTED' | 'DERIVED',
  valueSchema: typeof moneySchema = moneySchema,
) {
  return z
    .object({
      availability: z.literal(availability),
      value: valueSchema,
      lineageRef: z.string().min(1).max(200),
    })
    .strict();
}

/** 构造制度未披露、来源缺失或不适用的空金额事实 schema。 */
function unavailableMoneyFactSchema(
  availability: 'NOT_DISCLOSED_BY_REGIME' | 'SOURCE_MISSING' | 'NOT_APPLICABLE',
) {
  return z
    .object({
      availability: z.literal(availability),
      value: z.null(),
      lineageRef: z.string().min(1).max(200).nullable(),
    })
    .strict();
}

/** 校验金额值、币种、可用性和 lineage 必须保持一致。 */
export const stockConnectMoneyFactSchema = z.discriminatedUnion('availability', [
  availableMoneyFactSchema('REPORTED'),
  availableMoneyFactSchema('DERIVED'),
  unavailableMoneyFactSchema('NOT_DISCLOSED_BY_REGIME'),
  unavailableMoneyFactSchema('SOURCE_MISSING'),
  unavailableMoneyFactSchema('NOT_APPLICABLE'),
]);

/** 校验只能由来源报告的非负金额，禁止派生值或负数穿越服务边界。 */
const reportedNonNegativeMoneyFactSchema = z.discriminatedUnion('availability', [
  availableMoneyFactSchema('REPORTED', nonNegativeMoneySchema),
  unavailableMoneyFactSchema('NOT_DISCLOSED_BY_REGIME'),
  unavailableMoneyFactSchema('SOURCE_MISSING'),
  unavailableMoneyFactSchema('NOT_APPLICABLE'),
]);

/** 校验只能由同源买卖字段派生的有符号净额，禁止上游冒充直接报告值。 */
const derivedSignedMoneyFactSchema = z.discriminatedUnion('availability', [
  availableMoneyFactSchema('DERIVED'),
  unavailableMoneyFactSchema('NOT_DISCLOSED_BY_REGIME'),
  unavailableMoneyFactSchema('SOURCE_MISSING'),
  unavailableMoneyFactSchema('NOT_APPLICABLE'),
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
  const buy = facts.buyAmount.availability === 'REPORTED' ? facts.buyAmount.value : null;
  const sell = facts.sellAmount.availability === 'REPORTED' ? facts.sellAmount.value : null;
  const turnover =
    facts.turnoverAmount.availability === 'REPORTED' ? facts.turnoverAmount.value : null;
  const net = facts.netBuyAmount.availability === 'DERIVED' ? facts.netBuyAmount.value : null;

  if (buy !== null && sell !== null) {
    if (
      turnover === null ||
      !sameCurrency(buy, sell, turnover) ||
      !decimalIdentityHolds(buy.amount, sell.amount, turnover.amount, 'ADD')
    ) {
      context.addIssue({
        code: 'custom',
        path: ['turnoverAmount'],
        message: 'reported turnover must equal buy plus sell in one currency',
      });
    }
    if (
      net === null ||
      !sameCurrency(buy, sell, net) ||
      !decimalIdentityHolds(buy.amount, sell.amount, net.amount, 'SUBTRACT')
    ) {
      context.addIssue({
        code: 'custom',
        path: ['netBuyAmount'],
        message: 'derived net amount must equal buy minus sell in one currency',
      });
    }
    return;
  }

  if (net !== null) {
    context.addIssue({
      code: 'custom',
      path: ['netBuyAmount'],
      message: 'derived net amount requires reported buy and sell inputs',
    });
  }
}

/** 判断三个金额是否具有相同原币和基础单位。 */
function sameCurrency(
  left: z.infer<typeof moneySchema>,
  right: z.infer<typeof moneySchema>,
  result: z.infer<typeof moneySchema>,
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
  operation: 'ADD' | 'SUBTRACT',
): boolean {
  const scale = Math.max(decimalScale(left), decimalScale(right), decimalScale(result));
  const leftInteger = scaledDecimalInteger(left, scale);
  const rightInteger = scaledDecimalInteger(right, scale);
  const resultInteger = scaledDecimalInteger(result, scale);
  return operation === 'ADD'
    ? leftInteger + rightInteger === resultInteger
    : leftInteger - rightInteger === resultInteger;
}

/** 返回十进制字符串的小数位数。 */
function decimalScale(value: string): number {
  return value.split('.', 2)[1]?.length ?? 0;
}

/** 把已通过有界正则校验的十进制字符串转换为指定小数位的整数。 */
function scaledDecimalInteger(value: string, scale: number): bigint {
  const negative = value.startsWith('-');
  const unsigned = negative ? value.slice(1) : value;
  const [whole = '0', fraction = ''] = unsigned.split('.', 2);
  const digits = BigInt(`${whole}${fraction}`);
  const aligned = digits * 10n ** BigInt(scale - fraction.length);
  return negative ? -aligned : aligned;
}

/** 构造官方报告或平台派生的非空数量事实 schema。 */
function availableCountFactSchema(availability: 'REPORTED' | 'DERIVED') {
  return z
    .object({
      availability: z.literal(availability),
      value: z.number().int().nonnegative(),
      lineageRef: z.string().min(1).max(200),
    })
    .strict();
}

/** 构造制度未披露、来源缺失或不适用的空数量事实 schema。 */
function unavailableCountFactSchema(
  availability: 'NOT_DISCLOSED_BY_REGIME' | 'SOURCE_MISSING' | 'NOT_APPLICABLE',
) {
  return z
    .object({
      availability: z.literal(availability),
      value: z.null(),
      lineageRef: z.string().min(1).max(200).nullable(),
    })
    .strict();
}

/** 校验数量、可用性和 lineage 必须保持一致，空值不能被转换为零。 */
const countFactSchema = z.discriminatedUnion('availability', [
  availableCountFactSchema('REPORTED'),
  availableCountFactSchema('DERIVED'),
  unavailableCountFactSchema('NOT_DISCLOSED_BY_REGIME'),
  unavailableCountFactSchema('SOURCE_MISSING'),
  unavailableCountFactSchema('NOT_APPLICABLE'),
]);

/** 校验市场级统计，并保留买卖、成交额与净额的独立语义。 */
const marketStatsSchema = z
  .object({
    buyAmount: reportedNonNegativeMoneyFactSchema,
    sellAmount: reportedNonNegativeMoneyFactSchema,
    turnoverAmount: reportedNonNegativeMoneyFactSchema,
    netBuyAmount: derivedSignedMoneyFactSchema,
    tradeCount: countFactSchema,
    etfTurnoverAmount: reportedNonNegativeMoneyFactSchema,
  })
  .strict()
  .superRefine(validateMoneyIdentity);

/** 校验日终通道状态中与额度无关的共有字段。 */
const channelStatusFields = {
  tradingDay: z.boolean(),
  sessionState: z.enum(['OPEN', 'CLOSED', 'HALTED', 'NOT_OPEN', 'UNKNOWN']),
  buyOrderAccepted: z.boolean().nullable(),
  sellOrderAccepted: z.boolean().nullable(),
  observedAt: dateTimeSchema,
  finality: z.literal('END_OF_DAY'),
} as const;

/** 校验明确报告且固定使用人民币基础单位的额度余额。 */
const reportedCnyQuotaBalanceSchema = z
  .object({
    availability: z.literal('REPORTED'),
    value: z
      .object({
        amount: nonNegativeDecimalStringSchema,
        currency: z.literal('CNY'),
        unit: z.literal('BASE'),
      })
      .strict(),
    lineageRef: z.string().min(1).max(200),
  })
  .strict();

/** 校验 quotaState、余额可用性与人民币币种的强绑定。 */
const channelStatusSchema = z.discriminatedUnion('quotaState', [
  z
    .object({
      ...channelStatusFields,
      quotaState: z.literal('SUFFICIENT'),
      quotaBalance: unavailableMoneyFactSchema('NOT_DISCLOSED_BY_REGIME'),
    })
    .strict(),
  z
    .object({
      ...channelStatusFields,
      quotaState: z.literal('ACTUAL_REPORTED'),
      quotaBalance: reportedCnyQuotaBalanceSchema,
    })
    .strict(),
  z
    .object({
      ...channelStatusFields,
      quotaState: z.literal('EXHAUSTED'),
      quotaBalance: reportedCnyQuotaBalanceSchema,
    })
    .strict(),
  z
    .object({
      ...channelStatusFields,
      quotaState: z.literal('SOURCE_MISSING'),
      quotaBalance: unavailableMoneyFactSchema('SOURCE_MISSING'),
    })
    .strict(),
  z
    .object({
      ...channelStatusFields,
      quotaState: z.literal('NOT_APPLICABLE'),
      quotaBalance: unavailableMoneyFactSchema('NOT_APPLICABLE'),
    })
    .strict(),
]);

/** 校验来源引用中与 publication 时间可用性无关的共有字段。 */
const sourceRefFields = {
  sourceCode: z.enum([
    'HKEX_DATA_MARKETPLACE',
    'HKEX_OMDC',
    'HKEX_CALENDAR',
    'SSE_MDGW',
    'SZSE_STEP',
  ]),
  productName: z.string().min(1).max(160),
  sourceObservedAt: dateTimeSchema,
  sourceFileSha256: z
    .string()
    .regex(/^[a-f0-9]{64}$/)
    .nullable(),
} as const;

/** 校验来源 publication 时间与接收观察时间分离，禁止用 SFTP mtime 冒充发布。 */
const sourceRefSchema = z.discriminatedUnion('sourcePublicationAvailability', [
  z
    .object({
      ...sourceRefFields,
      sourcePublicationAvailability: z.literal('REPORTED'),
      sourcePublicationAt: dateTimeSchema,
    })
    .strict(),
  z
    .object({
      ...sourceRefFields,
      sourcePublicationAvailability: z.literal('NOT_PROVIDED_BY_SOURCE'),
      sourcePublicationAt: z.null(),
    })
    .strict(),
]);

/** 校验已批准 bundle publication 及其质量和真实来源证据。 */
const publicationSchema = z
  .object({
    bundleReleaseId: z.string().uuid(),
    dataVersion: z.string().min(1).max(160),
    tradeDate: dateSchema,
    publishedAt: dateTimeSchema,
    qualityStatus: z.enum(['APPROVED', 'APPROVED_WITH_WARNINGS']),
    qualityIssues: z
      .array(
        z
          .object({
            code: z.enum([
              'IDENTITY_SOURCE_UNRESOLVED',
              'SESSION_STATE_DERIVED_FROM_CALENDAR_AND_FINALITY',
              'STATUS_SOURCE_NOT_AVAILABLE_HISTORICAL',
              'OPTIONAL_FIELD_SOURCE_MISSING',
            ]),
            component: z.string().min(1).max(120),
            detail: z.string().min(1).max(300),
          })
          .strict(),
      )
      .max(20),
    sourceRefs: z.array(sourceRefSchema).min(1).max(12),
  })
  .strict();

/** 校验 readiness 使用的官方交易日历证据，不把观察时间冒充来源发布时间。 */
const readinessCalendarSchema = z
  .object({
    dataVersion: sha256Schema,
    observedAt: dateTimeSchema.nullable(),
    sourceFileSha256: sha256Schema.nullable(),
    sourcePublicationAt: dateTimeSchema.nullable(),
    publicationAvailability: z.enum(['REPORTED', 'NOT_REPORTED', 'SOURCE_MISSING']),
  })
  .strict()
  .superRefine((calendar, context) => {
    const hasReportedPublication = calendar.publicationAvailability === 'REPORTED';
    if (hasReportedPublication !== (calendar.sourcePublicationAt !== null)) {
      context.addIssue({
        code: 'custom',
        path: ['sourcePublicationAt'],
        message: 'calendar publication availability does not match timestamp',
      });
    }
  });

/** 校验候选交易日上一条通道的持久化 calendar、run 与 publication 证据。 */
const readinessChannelSchema = z
  .object({
    channel: channelSchema,
    calendarState: z.enum(['OPEN', 'CLOSED', 'UNKNOWN']),
    state: z.enum(STOCK_CONNECT_READINESS_STATES),
    reasonCode: z.enum(STOCK_CONNECT_READINESS_REASON_CODES),
    bundleDataVersion: z.string().min(1).max(160).nullable(),
    evidenceObservedAt: dateTimeSchema,
  })
  .strict()
  .superRefine((item, context) => {
    if (!READINESS_REASONS_BY_STATE[item.state].has(item.reasonCode)) {
      context.addIssue({
        code: 'custom',
        path: ['reasonCode'],
        message: 'readiness reason does not match state',
      });
    }
    if ((item.state === 'READY') !== (item.bundleDataVersion !== null)) {
      context.addIssue({
        code: 'custom',
        path: ['bundleDataVersion'],
        message: 'only a ready channel may expose a bundle version',
      });
    }
    if (item.state === 'READY' && item.calendarState !== 'OPEN') {
      context.addIssue({
        code: 'custom',
        path: ['calendarState'],
        message: 'a ready channel requires an official open day',
      });
    }
    if (
      item.state === 'NOT_TRADING' &&
      (item.calendarState !== 'CLOSED' || item.reasonCode !== 'OFFICIAL_CALENDAR_CLOSED')
    ) {
      context.addIssue({
        code: 'custom',
        path: ['calendarState'],
        message: 'a non-trading channel requires official closed calendar evidence',
      });
    }
  });

/** 校验 readiness 的日期模式、稳定通道矩阵、证据最大时间和 ready 日期语义。 */
function validateReadinessResponse(
  response: {
    mode: 'LATEST' | 'EXACT';
    selectedChannels: Array<z.infer<typeof channelSchema>>;
    requestedExactDate: string | null;
    candidateTradeDate: string | null;
    readyTradeDate: string | null;
    observedAt: string;
    calendar: z.infer<typeof readinessCalendarSchema>;
    channels: Array<z.infer<typeof readinessChannelSchema>>;
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
      code: 'custom',
      path: ['channels'],
      message: 'readiness channels must exactly match the stable selected channel order',
    });
  }

  const allReady = response.channels.every((item) => item.state === 'READY');
  if (response.mode === 'EXACT') {
    if (
      response.requestedExactDate === null ||
      (response.candidateTradeDate !== null &&
        response.candidateTradeDate !== response.requestedExactDate) ||
      (allReady && response.candidateTradeDate !== response.requestedExactDate) ||
      response.readyTradeDate !== (allReady ? response.requestedExactDate : null)
    ) {
      context.addIssue({
        code: 'custom',
        path: ['requestedExactDate'],
        message: 'exact readiness dates are inconsistent',
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
      code: 'custom',
      path: ['readyTradeDate'],
      message: 'latest readiness dates are inconsistent',
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
      code: 'custom',
      path: ['observedAt'],
      message: 'readiness observed time must equal the latest persisted evidence event',
    });
  }
}

/** 校验独立版本化 readiness 快照，失败和来源缺失同样只能来自已持久化证据。 */
export const stockConnectReadinessResponseSchema = z
  .object({
    schemaVersion: z.literal('quant-v2.stock-connect-readiness.v1'),
    dataVersion: sha256Schema,
    mode: z.enum(['LATEST', 'EXACT']),
    selectedChannels: z
      .array(channelSchema)
      .min(1)
      .max(4)
      .refine(hasUniqueChannels, 'selected channels must be unique'),
    requestedExactDate: dateSchema.nullable(),
    candidateTradeDate: dateSchema.nullable(),
    readyTradeDate: dateSchema.nullable(),
    observedAt: dateTimeSchema,
    calendar: readinessCalendarSchema,
    channels: z.array(readinessChannelSchema).min(1).max(4),
  })
  .strict()
  .superRefine(validateReadinessResponse);

/** 校验单条通道在一个精确交易日的汇总事实。 */
const channelSummarySchema = z
  .object({
    channel: channelSchema,
    direction: z.enum(['NORTHBOUND', 'SOUTHBOUND']),
    route: z.enum(['SHANGHAI', 'SHENZHEN']),
    tradeDate: dateSchema,
    stats: marketStatsSchema,
    status: channelStatusSchema,
    activeSecurityCount: z.number().int().min(0).max(10),
  })
  .strict();

/** 校验带通道身份的历史趋势点，避免总览多通道序列发生歧义。 */
const trendPointSchema = z
  .object({
    channel: channelSchema,
    tradeDate: dateSchema,
    dataVersion: z.string().min(1).max(160),
    stats: marketStatsSchema,
    status: channelStatusSchema,
  })
  .strict();

/** 校验总览当前点版本、跨通道同日版本及通道集合一致。 */
function validateOverviewResponse(
  response: {
    resolvedTradeDate: string;
    channels: Array<z.infer<typeof channelSummarySchema>>;
    trend: Array<z.infer<typeof trendPointSchema>>;
    publication: z.infer<typeof publicationSchema>;
  },
  context: z.RefinementCtx,
): void {
  if (response.publication.tradeDate !== response.resolvedTradeDate) {
    context.addIssue({ code: 'custom', message: 'overview publication date mismatch' });
  }
  const channels = new Set<string>();
  for (const channel of response.channels) channels.add(channel.channel);
  const versionsByDate = new Map<string, string>();
  for (const point of response.trend) {
    if (!channels.has(point.channel)) {
      context.addIssue({ code: 'custom', message: 'overview trend channel is not selected' });
    }
    const knownVersion = versionsByDate.get(point.tradeDate);
    if (knownVersion !== undefined && knownVersion !== point.dataVersion) {
      context.addIssue({ code: 'custom', message: 'overview same-day versions must match' });
    }
    versionsByDate.set(point.tradeDate, point.dataVersion);
    if (
      point.tradeDate === response.resolvedTradeDate &&
      point.dataVersion !== response.publication.dataVersion
    ) {
      context.addIssue({ code: 'custom', message: 'overview current trend version mismatch' });
    }
  }
}

/** 校验四通道共同 publication 的互联互通总览。 */
export const stockConnectOverviewResponseSchema = z
  .object({
    resolvedTradeDate: dateSchema,
    dateResolution: z.enum(['LATEST_COMMON', 'EXACT']),
    channels: z.array(channelSummarySchema).min(1).max(4),
    trend: z.array(trendPointSchema).max(1000),
    publication: publicationSchema,
  })
  .strict()
  .superRefine(validateOverviewResponse);

/** 校验单通道趋势只含该通道，且当前点与外层 publication 同版本。 */
function validateChannelResponse(
  response: {
    resolvedTradeDate: string;
    channel: z.infer<typeof channelSummarySchema>;
    trend: Array<z.infer<typeof trendPointSchema>>;
    publication: z.infer<typeof publicationSchema>;
  },
  context: z.RefinementCtx,
): void {
  if (
    response.publication.tradeDate !== response.resolvedTradeDate ||
    response.channel.tradeDate !== response.resolvedTradeDate
  ) {
    context.addIssue({ code: 'custom', message: 'channel publication date mismatch' });
  }
  for (const point of response.trend) {
    if (point.channel !== response.channel.channel) {
      context.addIssue({ code: 'custom', message: 'channel trend contains another channel' });
    }
    if (
      point.tradeDate === response.resolvedTradeDate &&
      point.dataVersion !== response.publication.dataVersion
    ) {
      context.addIssue({ code: 'custom', message: 'channel current trend version mismatch' });
    }
  }
}

/** 校验单通道日终详情及其有界历史趋势。 */
export const stockConnectChannelResponseSchema = z
  .object({
    resolvedTradeDate: dateSchema,
    dateResolution: z.enum(['LATEST_CHANNEL', 'EXACT']),
    channel: channelSummarySchema,
    trend: z.array(trendPointSchema).max(250),
    publication: publicationSchema,
  })
  .strict()
  .superRefine(validateChannelResponse);

/** 校验证券稳定身份或明确的来源未解析降级，并强绑定 entityRef 空值语义。 */
const instrumentIdentitySchema = z.discriminatedUnion('identityAvailability', [
  z
    .object({
      identityAvailability: z.literal('RESOLVED'),
      instrumentEntityRef: z.string().min(1).max(160),
      sourceSecurityCode: z.string().min(1).max(32),
      displayName: z.string().max(160).nullable(),
      listingVenue: z.enum(['SSE', 'SZSE', 'HKEX']),
    })
    .strict(),
  z
    .object({
      identityAvailability: z.literal('SOURCE_UNRESOLVED'),
      instrumentEntityRef: z.null(),
      sourceSecurityCode: z.string().min(1).max(32),
      displayName: z.string().max(160).nullable(),
      listingVenue: z.enum(['SSE', 'SZSE', 'HKEX']),
    })
    .strict(),
]);

/** 校验官方来源活跃榜中的单只证券，不把该记录扩张为全市场排行。 */
const activeSecuritySchema = z
  .object({
    rankingRank: z.number().int().min(1).max(10),
    sourceRank: z.number().int().min(1).max(10),
    identity: instrumentIdentitySchema,
    buyAmount: reportedNonNegativeMoneyFactSchema,
    sellAmount: reportedNonNegativeMoneyFactSchema,
    turnoverAmount: reportedNonNegativeMoneyFactSchema,
    netBuyAmount: derivedSignedMoneyFactSchema,
  })
  .strict()
  .superRefine(validateMoneyIdentity);

/** 校验活跃榜类型、可用性、空页和确定性名次不能互相矛盾。 */
function validateActiveSecurityPage(
  page: {
    resolvedTradeDate: string;
    ranking: (typeof STOCK_CONNECT_RANKINGS)[number];
    rankingAvailability: (typeof STOCK_CONNECT_AVAILABILITIES)[number];
    items: Array<z.infer<typeof activeSecuritySchema>>;
    nextCursor: string | null;
    publication: z.infer<typeof publicationSchema>;
  },
  context: z.RefinementCtx,
): void {
  if (page.publication.tradeDate !== page.resolvedTradeDate) {
    context.addIssue({ code: 'custom', message: 'active ranking publication date mismatch' });
  }
  if (page.ranking === 'SOURCE_ACTIVE' && page.rankingAvailability !== 'REPORTED') {
    context.addIssue({ code: 'custom', message: 'source active ranking must be reported' });
  }
  if (
    page.ranking !== 'SOURCE_ACTIVE' &&
    !['DERIVED', 'NOT_DISCLOSED_BY_REGIME', 'SOURCE_MISSING', 'NOT_APPLICABLE'].includes(
      page.rankingAvailability,
    )
  ) {
    context.addIssue({ code: 'custom', message: 'net ranking availability is invalid' });
  }
  if (
    page.ranking !== 'SOURCE_ACTIVE' &&
    page.rankingAvailability !== 'DERIVED' &&
    (page.items.length > 0 || page.nextCursor !== null)
  ) {
    context.addIssue({ code: 'custom', message: 'unavailable net ranking must be an empty page' });
  }
  if (
    page.ranking !== 'SOURCE_ACTIVE' &&
    page.rankingAvailability === 'DERIVED' &&
    hasNonDerivedNetFact(page.items)
  ) {
    context.addIssue({ code: 'custom', message: 'net ranking items require derived net facts' });
  }
  if (page.ranking === 'SOURCE_ACTIVE' && hasChangedSourceRank(page.items)) {
    context.addIssue({ code: 'custom', message: 'source ranking rank must preserve source rank' });
  }
  for (let index = 1; index < page.items.length; index += 1) {
    if (page.items[index]!.rankingRank <= page.items[index - 1]!.rankingRank) {
      context.addIssue({ code: 'custom', message: 'ranking ranks must be strictly increasing' });
      break;
    }
  }
}

/** 判断净额排行是否混入未派生的净额事实。 */
function hasNonDerivedNetFact(items: Array<z.infer<typeof activeSecuritySchema>>): boolean {
  for (const item of items) {
    if (item.netBuyAmount.availability !== 'DERIVED') return true;
  }
  return false;
}

/** 判断来源活跃榜是否错误改写了官方来源名次。 */
function hasChangedSourceRank(items: Array<z.infer<typeof activeSecuritySchema>>): boolean {
  for (const item of items) {
    if (item.rankingRank !== item.sourceRank) return true;
  }
  return false;
}

/** 校验官方活跃证券榜和仅在该榜内计算的净额排序。 */
export const stockConnectActiveSecurityPageSchema = z
  .object({
    resolvedTradeDate: dateSchema,
    dateResolution: z.enum(['LATEST_COMMON', 'LATEST_CHANNEL', 'EXACT']),
    channel: channelSchema,
    ranking: z.enum(STOCK_CONNECT_RANKINGS),
    rankingAvailability: availabilitySchema,
    rankingScope: z.literal('SOURCE_ACTIVE_SECURITIES_ONLY'),
    items: z.array(activeSecuritySchema).max(100),
    nextCursor: z.string().max(1024).nullable(),
    publication: publicationSchema,
  })
  .strict()
  .superRefine(validateActiveSecurityPage);

/** 校验证券只在互联互通来源活跃榜内出现的历史表现。 */
const securityChannelActivitySchema = z
  .object({
    channel: channelSchema,
    tradeDate: dateSchema,
    dataVersion: z.string().min(1).max(160),
    sourceRank: z.number().int().min(1).max(10),
    turnoverAmount: reportedNonNegativeMoneyFactSchema,
    netBuyAmount: derivedSignedMoneyFactSchema,
  })
  .strict();

/** 校验证券活动同日跨通道版本一致，当前日与外层 publication 一致。 */
function validateSecurityContextResponse(
  response: {
    resolvedTradeDate: string;
    activities: Array<z.infer<typeof securityChannelActivitySchema>>;
    publication: z.infer<typeof publicationSchema>;
  },
  context: z.RefinementCtx,
): void {
  if (response.publication.tradeDate !== response.resolvedTradeDate) {
    context.addIssue({ code: 'custom', message: 'security context publication date mismatch' });
  }
  const versionsByDate = new Map<string, string>();
  for (const activity of response.activities) {
    const knownVersion = versionsByDate.get(activity.tradeDate);
    if (knownVersion !== undefined && knownVersion !== activity.dataVersion) {
      context.addIssue({ code: 'custom', message: 'security same-day versions must match' });
    }
    versionsByDate.set(activity.tradeDate, activity.dataVersion);
    if (
      activity.tradeDate === response.resolvedTradeDate &&
      activity.dataVersion !== response.publication.dataVersion
    ) {
      context.addIssue({ code: 'custom', message: 'security current activity version mismatch' });
    }
  }
}

/** 校验证券稳定身份及其跨通道、按交易日分隔的历史表现。 */
export const stockConnectSecurityContextResponseSchema = z
  .object({
    resolvedTradeDate: dateSchema,
    identity: instrumentIdentitySchema,
    activities: z.array(securityChannelActivitySchema).max(1000),
    publication: publicationSchema,
  })
  .strict()
  .superRefine(validateSecurityContextResponse);

/** 校验 data-sync 内部 RFC 9457 错误，禁止未知字段或原始异常泄漏。 */
export const internalStockConnectProblemSchema = z
  .object({
    type: z.string().url(),
    title: z.string(),
    status: z.number().int().min(400).max(599),
    detail: z.string().max(500),
    instance: z.string().min(1).max(2048),
    code: z.enum([
      'VALIDATION_FAILED',
      'AUTHENTICATION_FAILED',
      'EXACT_DATE_NOT_PUBLISHED',
      'PUBLICATION_NOT_READY',
      'READINESS_NOT_OBSERVED',
      'CURSOR_VERSION_MISMATCH',
      'PARENT_PUBLICATION_MISMATCH',
      'DATA_SOURCE_UNAVAILABLE',
      'INTERNAL_DEPENDENCY_FAILED',
      'SECURITY_CONTEXT_NOT_FOUND',
    ]),
    requestId: z.string().max(128),
  })
  .strict();

/** 描述互联互通总览请求。 */
export type StockConnectOverviewQuery = z.infer<typeof stockConnectOverviewQuerySchema>;

/** 描述独立于业务 publication 的通道 readiness 查询。 */
export type StockConnectReadinessQuery = z.infer<typeof stockConnectReadinessQuerySchema>;

/** 描述单通道详情请求。 */
export type StockConnectChannelQuery = z.infer<typeof stockConnectChannelQuerySchema>;

/** 描述活跃证券榜请求。 */
export type StockConnectActiveSecurityQuery = z.infer<typeof stockConnectActiveSecurityQuerySchema>;

/** 描述证券互联互通上下文请求。 */
export type StockConnectSecurityContextQuery = z.infer<
  typeof stockConnectSecurityContextQuerySchema
>;

/** 描述互联互通总览成功响应。 */
export type StockConnectOverviewResponse = z.infer<typeof stockConnectOverviewResponseSchema>;

/** 描述候选交易日、共同 ready 日与逐通道持久化准备状态。 */
export type StockConnectReadinessResponse = z.infer<typeof stockConnectReadinessResponseSchema>;

/** 描述单通道详情成功响应。 */
export type StockConnectChannelResponse = z.infer<typeof stockConnectChannelResponseSchema>;

/** 描述活跃证券游标页成功响应。 */
export type StockConnectActiveSecurityPage = z.infer<typeof stockConnectActiveSecurityPageSchema>;

/** 描述证券互联互通上下文成功响应。 */
export type StockConnectSecurityContextResponse = z.infer<
  typeof stockConnectSecurityContextResponseSchema
>;
