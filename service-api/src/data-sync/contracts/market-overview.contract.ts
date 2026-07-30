import { z } from 'zod';

/** 约束跨服务传输的十进制字符串，避免金额、点位和百分比经过 JSON number 丢失精度。 */
export const marketDecimalSchema = z
  .string()
  .regex(/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/, 'must be a canonical decimal string');

/** 约束市场数据采用带时区偏移的观测或发布时间。 */
const offsetDateTimeSchema = z.string().datetime({ offset: true });

/** 约束市场数据只接受日历日期，不允许时间戳混入交易日维度。 */
const dateOnlySchema = z.string().regex(/^\d{4}-\d{2}-\d{2}$/);

/** 冻结四个主要指数的供应商中立稳定身份。 */
const primaryMarketIndexIdSchema = z.enum([
  'sse-composite',
  'szse-component',
  'csi-300',
  'chinext',
]);

/** 描述一项可审计的 Tushare 来源绑定，不暴露 token 或 raw 对象地址。 */
export const marketSourceSchema = z
  .object({
    provider: z.literal('tushare-pro'),
    upstreamSource: z.string().min(1).max(128),
    sourceDataset: z.string().min(1).max(128),
    observedAt: offsetDateTimeSchema,
    adapterVersion: z.string().min(1).max(64),
    schemaFingerprint: z.string().regex(/^[a-f0-9]{64}$/),
  })
  .strict();

/** 区分原始来源组件与平台派生组件，避免把内部计算伪装为 Tushare 直报事实。 */
const marketSourceBindingSchema = z.discriminatedUnion('role', [
  marketSourceSchema
    .extend({
      role: z.literal('external'),
      component: z.string().min(1).max(128),
    })
    .strict(),
  z
    .object({
      role: z.literal('derived'),
      component: z.string().min(1).max(128),
      provider: z.literal('quant-v2-derivation'),
      upstreamSource: z.string().min(1).max(128),
      sourceDataset: z.string().min(1).max(128),
      observedAt: offsetDateTimeSchema,
      adapterVersion: z.string().min(1).max(64),
      schemaFingerprint: z.string().regex(/^[a-f0-9]{64}$/),
      methodology: z
        .object({
          id: z.string().min(1).max(128),
          version: z.string().min(1).max(64),
          status: z.literal('platform_derived'),
        })
        .strict(),
    })
    .strict(),
]);

/** 约束 P0 沪深 A 股公开身份，禁止北交所越过冻结 universe 或按代码前缀猜交易所。 */
const marketOverviewEquityIdentitySchema = z
  .object({
    exchange: z.enum(['SSE', 'SZSE']),
    symbol: z.string().regex(/^\d{6}$/),
    name: z.string().min(1).max(128),
  })
  .strict();

/** 约束申万正式成员身份；该独立 taxonomy 不继承市场首页沪深 universe。 */
const swConstituentIdentitySchema = z
  .object({
    exchange: z.enum(['SSE', 'SZSE', 'BSE']),
    symbol: z.string().regex(/^\d{6}$/),
    name: z.string().min(1).max(128),
  })
  .strict();

/** 描述首页或独立排行中的证券 EOD 指标。 */
export const marketEquityRankItemSchema = marketOverviewEquityIdentitySchema
  .extend({
    rank: z.number().int().min(1),
    close: marketDecimalSchema,
    changePercent: marketDecimalSchema,
    amountCny: marketDecimalSchema,
    turnoverPercent: marketDecimalSchema.nullable(),
  })
  .strict();

/** 描述供应商订单规模方法学下的一条证券资金流排名。 */
const moneyFlowRankItemSchema = marketOverviewEquityIdentitySchema
  .extend({
    rank: z.number().int().min(1),
    netAmountCny: marketDecimalSchema,
    buyLargeAmountCny: marketDecimalSchema.nullable(),
    sellLargeAmountCny: marketDecimalSchema.nullable(),
    changePercent: marketDecimalSchema.nullable(),
  })
  .strict();

/** 描述同一板块 publication 内的一条强弱排名。 */
const sectorRankItemSchema = z
  .object({
    rank: z.number().int().min(1),
    sectorCode: z.string().min(1).max(64),
    name: z.string().min(1).max(128),
    changePercent: marketDecimalSchema,
    turnoverPercent: marketDecimalSchema.nullable(),
    amountCny: marketDecimalSchema.nullable(),
    leadingEquity: marketOverviewEquityIdentitySchema
      .extend({ changePercent: marketDecimalSchema })
      .strict()
      .nullable(),
    validSamples: z.number().int().min(1),
  })
  .strict();

/** 描述一个证据化市场关注信号，禁止返回无阈值依据的自然语言判断。 */
const attentionSignalSchema = z
  .object({
    signalId: z.string().min(1).max(128),
    ruleId: z.string().min(1).max(128),
    rulesVersion: z.literal('1'),
    severity: z.enum(['info', 'warning']),
    title: z.string().min(1).max(256),
    evidence: z
      .array(
        z
          .object({
            metric: z.string().min(1).max(128),
            currentValue: marketDecimalSchema,
            threshold: marketDecimalSchema,
            unit: z.string().min(1).max(64),
          })
          .strict(),
      )
      .min(1)
      .max(32),
  })
  .strict();

/** 描述首页完整包的单项质量检查。 */
const marketQualityCheckSchema = z
  .object({
    code: z.string().min(1).max(128),
    status: z.literal('passed'),
    actual: z.string().max(256),
    expected: z.string().max(256),
  })
  .strict();

/** 严格校验市场首页原子完整包；任何必需组件缺失都会被视为下游合同漂移。 */
export const marketOverviewSchema = z
  .object({
    dataVersion: z.string().uuid(),
    tradeDate: dateOnlySchema,
    publishedAt: offsetDateTimeSchema,
    finality: z.literal('final'),
    status: z
      .object({
        marketState: z.enum(['pre_open', 'trading', 'lunch_break', 'closed', 'non_trading_day']),
        marketStateAsOf: offsetDateTimeSchema,
        marketStateMethodology: z.literal('calendar_schedule_derived'),
        freshness: z.enum(['current', 'stale']),
        latestEligibleTradeDate: dateOnlySchema,
        latestAttemptedTradeDate: dateOnlySchema.nullable(),
        lagTradingDays: z.number().int().min(0),
        eodEligibilityScheduleVersion: z.literal('cn-a-eod-eligibility-2026-v1'),
        freshnessReason: z.enum([
          'latest_eligible_complete',
          'latest_eligible_bundle_incomplete',
          'latest_eligible_bundle_unavailable',
          'publication_rollback',
          'historical_snapshot',
        ]),
        quality: z.literal('passed'),
      })
      .strict(),
    indices: z
      .array(
        z
          .object({
            indexId: primaryMarketIndexIdSchema,
            name: z.string().min(1).max(128),
            point: marketDecimalSchema,
            previousClose: marketDecimalSchema,
            change: marketDecimalSchema,
            changePercent: marketDecimalSchema,
            open: marketDecimalSchema,
            high: marketDecimalSchema,
            low: marketDecimalSchema,
            volume: marketDecimalSchema.nullable(),
            volumeUnit: z.literal('lot'),
            amountCny: marketDecimalSchema.nullable(),
            source: marketSourceSchema,
          })
          .strict(),
      )
      .length(4)
      .refine(
        hasAllPrimaryIndexIds,
        'all four primary index identities must be present exactly once',
      ),
    turnover: z
      .object({
        label: z.literal('沪深 A 股成交额'),
        universe: z.literal('CN-A-SSE-SZSE'),
        methodologyId: z.literal('sum-tushare-daily-a-share-amount-cny-v1'),
        sseAmountCny: marketDecimalSchema,
        szseAmountCny: marketDecimalSchema,
        totalAmountCny: marketDecimalSchema,
        previousTotalAmountCny: marketDecimalSchema,
        changeAmountCny: marketDecimalSchema,
        changePercent: marketDecimalSchema,
      })
      .strict(),
    breadth: z
      .object({
        eligible: z.number().int().min(0),
        advancing: z.number().int().min(0),
        flat: z.number().int().min(0),
        declining: z.number().int().min(0),
        suspended: z.number().int().min(0),
        unknown: z.literal(0),
      })
      .strict(),
    limits: z
      .object({
        limitUp: z.number().int().min(0),
        limitDown: z.number().int().min(0),
        rulesVersion: z.string().min(1).max(64),
      })
      .strict(),
    marketMoneyFlow: z
      .object({
        source: marketSourceSchema,
        methodologyId: z.string().min(1).max(128),
        methodologyVersion: z.string().min(1).max(64),
        netAmountCny: marketDecimalSchema,
      })
      .strict(),
    equityMoneyFlowRankings: z
      .object({
        source: marketSourceSchema,
        methodologyId: z.literal('tushare-order-size-flow'),
        methodologyVersion: z.literal('1'),
        universe: z.literal('CN-A-SSE-SZSE-TRADED'),
        coverage: marketDecimalSchema.refine(
          isUnitIntervalDecimal,
          'coverage must be between zero and one',
        ),
        inflow: z.array(moneyFlowRankItemSchema).max(50),
        outflow: z.array(moneyFlowRankItemSchema).max(50),
      })
      .strict(),
    equityRankings: z
      .object({
        gainers: z.array(marketEquityRankItemSchema).max(50),
        losers: z.array(marketEquityRankItemSchema).max(50),
        amount: z.array(marketEquityRankItemSchema).max(50),
        turnover: z.array(marketEquityRankItemSchema).max(50),
      })
      .strict(),
    sectorRankings: z
      .object({
        eastmoneyIndustry: z
          .object({
            strongest: z.array(sectorRankItemSchema).max(50),
            weakest: z.array(sectorRankItemSchema).max(50),
          })
          .strict(),
        eastmoneyConcept: z
          .object({
            strongest: z.array(sectorRankItemSchema).max(50),
            weakest: z.array(sectorRankItemSchema).max(50),
          })
          .strict(),
      })
      .strict(),
    attentionSignals: z.array(attentionSignalSchema).max(100),
    quality: z
      .object({
        componentCount: z.number().int().min(1),
        passedCount: z.number().int().min(1),
        universeVersion: z.string().min(1).max(128),
        sourceBindings: z.array(marketSourceBindingSchema).min(1).max(128),
        checks: z.array(marketQualityCheckSchema).min(1).max(256),
      })
      .strict(),
  })
  .strict()
  // 完整包发布要求所有固定组件同时通过，拒绝 partial 结果越过 API 边界。
  .superRefine((value, context) => {
    const { freshness, freshnessReason, lagTradingDays } = value.status;
    if (
      freshness === 'current' &&
      (lagTradingDays !== 0 || freshnessReason !== 'latest_eligible_complete')
    ) {
      context.addIssue({
        code: 'custom',
        path: ['status', 'freshness'],
        message: 'current freshness requires an unlagged latest eligible complete bundle',
      });
    }
    if (freshness === 'stale' && freshnessReason === 'latest_eligible_complete') {
      context.addIssue({
        code: 'custom',
        path: ['status', 'freshnessReason'],
        message: 'stale freshness requires a non-complete or rollback reason',
      });
    }
    if (
      freshnessReason !== 'publication_rollback' &&
      freshnessReason !== 'historical_snapshot' &&
      freshnessReason !== 'latest_eligible_complete' &&
      lagTradingDays === 0
    ) {
      context.addIssue({
        code: 'custom',
        path: ['status', 'lagTradingDays'],
        message:
          'an incomplete or unavailable eligible bundle must lag by at least one trading day',
      });
    }
    if (
      freshnessReason === 'historical_snapshot' &&
      (freshness !== 'stale' ||
        value.status.marketState !== 'closed' ||
        lagTradingDays !== 0 ||
        value.status.latestEligibleTradeDate !== value.tradeDate ||
        value.status.latestAttemptedTradeDate !== null)
    ) {
      context.addIssue({
        code: 'custom',
        path: ['status'],
        message: 'historical status must describe a closed, unlagged frozen selected snapshot',
      });
    }
    if (value.quality.componentCount !== value.quality.passedCount) {
      context.addIssue({
        code: 'custom',
        path: ['quality', 'passedCount'],
        message: 'all overview components must pass before publication',
      });
    }
    if (value.quality.componentCount !== value.quality.sourceBindings.length) {
      context.addIssue({
        code: 'custom',
        path: ['quality', 'sourceBindings'],
        message: 'every complete overview component must retain one source binding',
      });
    }
    if (
      value.breadth.advancing +
        value.breadth.flat +
        value.breadth.declining +
        value.breadth.suspended !==
      value.breadth.eligible
    ) {
      context.addIssue({
        code: 'custom',
        path: ['breadth'],
        message: 'market breadth must reconcile to the eligible universe',
      });
    }
    for (const [index, item] of value.equityMoneyFlowRankings.inflow.entries()) {
      if (decimalSign(item.netAmountCny) !== 1) {
        context.addIssue({
          code: 'custom',
          path: ['equityMoneyFlowRankings', 'inflow', index, 'netAmountCny'],
          message: 'inflow rankings require a strictly positive net amount',
        });
      }
    }
    for (const [index, item] of value.equityMoneyFlowRankings.outflow.entries()) {
      if (decimalSign(item.netAmountCny) !== -1) {
        context.addIssue({
          code: 'custom',
          path: ['equityMoneyFlowRankings', 'outflow', index, 'netAmountCny'],
          message: 'outflow rankings require a strictly negative net amount',
        });
      }
    }
  });

/** 描述指数日 K 线中的一个来源直报交易日。 */
const marketIndexBarSchema = z
  .object({
    tradeDate: dateOnlySchema,
    open: marketDecimalSchema,
    high: marketDecimalSchema,
    low: marketDecimalSchema,
    close: marketDecimalSchema,
    previousClose: marketDecimalSchema,
    change: marketDecimalSchema,
    changePercent: marketDecimalSchema,
    volume: marketDecimalSchema.nullable(),
    amountCny: marketDecimalSchema.nullable(),
    finality: z.literal('final'),
  })
  .strict();

/** 严格校验一个固定指数身份与 publication 的日 K 线页。 */
export const marketIndexBarPageSchema = z
  .object({
    dataVersion: z.string().uuid(),
    publishedAt: offsetDateTimeSchema,
    index: z
      .object({
        indexId: primaryMarketIndexIdSchema,
        name: z.string().min(1).max(128),
      })
      .strict(),
    period: z.literal('1d'),
    volumeUnit: z.literal('lot'),
    source: marketSourceSchema,
    inputDataVersions: z
      .array(z.string().uuid())
      .min(1)
      .max(7_500)
      .refine(hasUniqueStrings, 'index bar input publication versions must be unique'),
    items: z.array(marketIndexBarSchema).max(1_000),
    nextCursor: z.string().min(1).max(2_048).nullable(),
  })
  .strict();

/** 严格校验冻结全市场横截面派生的证券排行页。 */
export const marketEquityRankingPageSchema = z
  .object({
    dataVersion: z.string().uuid(),
    tradeDate: dateOnlySchema,
    publishedAt: offsetDateTimeSchema,
    source: marketSourceSchema,
    metric: z.enum(['changePercent', 'amountCny', 'turnoverPercent']),
    order: z.enum(['asc', 'desc']),
    universe: z.literal('CN-A-SSE-SZSE-ELIGIBLE'),
    coverage: marketDecimalSchema.refine(
      isUnitIntervalDecimal,
      'coverage must be between zero and one',
    ),
    finality: z.literal('final'),
    quality: z
      .object({
        status: z.literal('passed'),
        universeVersion: z.string().min(1).max(128),
        checks: z.array(marketQualityCheckSchema).min(1).max(128),
      })
      .strict(),
    items: z.array(marketEquityRankItemSchema).max(50),
    nextCursor: z.string().min(1).max(2_048).nullable(),
  })
  .strict();

/** 严格校验 Tushare 订单规模方法学下的一侧证券资金流排行。 */
export const marketEquityMoneyFlowRankingPageSchema = z
  .object({
    dataVersion: z.string().uuid(),
    tradeDate: dateOnlySchema,
    publishedAt: offsetDateTimeSchema,
    source: marketSourceSchema,
    direction: z.enum(['inflow', 'outflow']),
    methodology: z
      .object({
        id: z.literal('tushare-order-size-flow'),
        version: z.literal('1'),
        semanticFamily: z.literal('order_size_flow'),
        status: z.literal('source_reported'),
      })
      .strict(),
    universe: z.literal('CN-A-SSE-SZSE-TRADED'),
    coverage: marketDecimalSchema.refine(
      isUnitIntervalDecimal,
      'coverage must be between zero and one',
    ),
    items: z.array(moneyFlowRankItemSchema).max(50),
    finality: z.literal('final'),
    quality: z
      .object({
        status: z.literal('passed'),
        checks: z.array(marketQualityCheckSchema).min(1).max(128),
      })
      .strict(),
    nextCursor: z.string().min(1).max(2_048).nullable(),
  })
  .strict()
  // 一侧排行只接受与请求方向一致的非零净额，禁止零值或反向值混入榜单。
  .superRefine((value, context) => {
    const expectedSign = value.direction === 'inflow' ? 1 : -1;
    for (const [index, item] of value.items.entries()) {
      if (decimalSign(item.netAmountCny) !== expectedSign) {
        context.addIssue({
          code: 'custom',
          path: ['items', index, 'netAmountCny'],
          message: `${value.direction} rankings require a direction-consistent non-zero net amount`,
        });
      }
    }
  });

/** 严格校验交易日历与 Asia/Shanghai 会话日程。 */
export const marketCalendarPageSchema = z
  .object({
    dataVersion: z.string().uuid(),
    publishedAt: offsetDateTimeSchema,
    timezone: z.literal('Asia/Shanghai'),
    sessionScheduleVersion: z.string().min(1).max(64),
    source: marketSourceSchema,
    quality: z
      .object({
        status: z.literal('passed'),
        checks: z.array(marketQualityCheckSchema).min(1).max(128),
      })
      .strict(),
    items: z
      .array(
        z
          .object({
            venue: z.enum(['SSE', 'SZSE']),
            tradeDate: dateOnlySchema,
            isTradingDay: z.boolean(),
            previousTradingDate: dateOnlySchema.nullable(),
            sessions: z
              .array(
                z
                  .object({
                    name: z.string().min(1).max(64),
                    start: z.string().regex(/^\d{2}:\d{2}:\d{2}$/),
                    end: z.string().regex(/^\d{2}:\d{2}:\d{2}$/),
                  })
                  .strict(),
              )
              .max(16),
          })
          .strict(),
      )
      .max(1_500),
  })
  .strict();

/** 严格校验同一分类体系与同一 publication 的板块强弱页。 */
export const marketSectorStrengthPageSchema = z
  .object({
    dataVersion: z.string().uuid(),
    tradeDate: dateOnlySchema,
    publishedAt: offsetDateTimeSchema,
    scheme: z.enum(['eastmoney.industry', 'eastmoney.concept']),
    window: z.union([z.literal(1), z.literal(5), z.literal(20)]),
    order: z.enum(['asc', 'desc']),
    methodologyVersion: z.string().min(1).max(64),
    source: marketSourceSchema,
    inputDataVersions: z
      .array(z.string().uuid())
      .min(1)
      .max(252)
      .refine(hasUniqueStrings, 'sector strength input publication versions must be unique'),
    quality: z
      .object({
        status: z.literal('passed'),
        validUniverseCount: z.number().int().min(1),
        checks: z.array(marketQualityCheckSchema).min(1).max(128),
      })
      .strict(),
    items: z
      .array(
        z
          .object({
            rank: z.number().int().min(1),
            sectorCode: z.string().min(1).max(64),
            name: z.string().min(1).max(128),
            changePercent: marketDecimalSchema,
            turnoverPercent: marketDecimalSchema.nullable(),
            amountCny: marketDecimalSchema.nullable(),
            cumulativeReturn: marketDecimalSchema,
            upDays: z.number().int().min(0),
            medianRank: marketDecimalSchema.nullable(),
            validSamples: z.number().int().min(1),
            coverage: z.literal('1'),
          })
          .strict(),
      )
      .min(1)
      .max(100),
    nextCursor: z.string().min(1).max(2_048).nullable(),
  })
  .strict()
  // 5/20 日强弱只接受同步阶段已验证的完整共同交易日窗口，任何部分样本均整体失败。
  .superRefine((value, context) => {
    if (value.inputDataVersions.length !== value.window) {
      context.addIssue({
        code: 'custom',
        path: ['inputDataVersions'],
        message: 'strength input publications must exactly match the requested trading-day window',
      });
    }
    if (value.quality.validUniverseCount < value.items.length) {
      context.addIssue({
        code: 'custom',
        path: ['quality', 'validUniverseCount'],
        message: 'valid universe count must cover every returned strength item',
      });
    }
    for (const [index, item] of value.items.entries()) {
      if (item.validSamples !== value.window) {
        context.addIssue({
          code: 'custom',
          path: ['items', index, 'validSamples'],
          message: 'validSamples must equal the requested complete strength window',
        });
      }
    }
  });

/** 严格校验一个东财分类体系的来源资金流排行 publication。 */
export const marketSectorMoneyFlowRankingPageSchema = z
  .object({
    dataVersion: z.string().uuid(),
    tradeDate: dateOnlySchema,
    publishedAt: offsetDateTimeSchema,
    scheme: z.enum(['eastmoney.industry', 'eastmoney.concept']),
    order: z.enum(['asc', 'desc']),
    source: marketSourceSchema,
    methodology: z
      .object({
        id: z.literal('eastmoney-sector-flow-dc'),
        version: z.literal('unknown'),
        semanticFamily: z.literal('trade_direction_flow'),
        status: z.literal('source_reported'),
        rankingBasis: z.literal('canonical_net_amount'),
      })
      .strict(),
    coverage: z.literal('1'),
    finality: z.literal('final'),
    quality: z
      .object({
        status: z.literal('passed'),
        validUniverseCount: z.number().int().min(1),
        checks: z.array(marketQualityCheckSchema).min(1).max(128),
      })
      .strict(),
    items: z
      .array(
        z
          .object({
            rank: z.number().int().min(1),
            sectorCode: z.string().min(1).max(64),
            name: z.string().min(1).max(128),
            close: marketDecimalSchema,
            changePercent: marketDecimalSchema,
            netAmountCny: marketDecimalSchema,
          })
          .strict(),
      )
      .min(1)
      .max(100),
    nextCursor: z.string().min(1).max(2_048).nullable(),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.quality.validUniverseCount < value.items.length) {
      context.addIssue({
        code: 'custom',
        path: ['quality', 'validUniverseCount'],
        message: 'valid universe count must cover every returned money-flow item',
      });
    }
  });

/** 严格校验申万行业同步期已物化并确认结束的日、周、月 K 线。 */
export const swIndustryBarPageSchema = z
  .object({
    dataVersion: z.string().uuid(),
    publishedAt: offsetDateTimeSchema,
    industry: z
      .object({
        code: z.string().regex(/^\d{6}\.SI$/),
        name: z.string().min(1).max(128),
        level: z.number().int().min(1).max(3),
        parentCode: z
          .string()
          .regex(/^\d{6}\.SI$/)
          .nullable(),
      })
      .strict(),
    period: z.enum(['1d', '1w', '1mo']),
    volumeUnit: z.literal('provider_native'),
    source: marketSourceSchema,
    methodology: z
      .object({
        id: z.enum(['source-reported-daily-bar', 'calendar-bounded-ohlcv-aggregation']),
        version: z.literal('1'),
        status: z.enum(['source_reported', 'platform_derived']),
        inputDataset: z.literal('sw.market-data'),
        previousClose: z.discriminatedUnion('id', [
          z
            .object({
              kind: z.literal('derived'),
              id: z.literal('sw-previous-close-from-close-change'),
              version: z.literal('1'),
              inputs: z.tuple([z.literal('close'), z.literal('change')]),
            })
            .strict(),
          z
            .object({
              kind: z.literal('derived'),
              id: z.literal('period-opening-previous-close-from-daily'),
              version: z.literal('1'),
              inputs: z.tuple([z.literal('daily.previousClose')]),
            })
            .strict(),
        ]),
      })
      .strict(),
    inputDataVersions: z
      .array(z.string().uuid())
      .min(1)
      .max(7_500)
      .refine(hasUniqueStrings, 'SW bar input publication versions must be unique'),
    finality: z.literal('final'),
    items: z
      .array(
        z
          .object({
            period: z.enum(['1d', '1w', '1mo']),
            periodKey: z.string().min(1).max(32),
            periodStart: dateOnlySchema,
            periodEnd: dateOnlySchema,
            open: marketDecimalSchema,
            high: marketDecimalSchema,
            low: marketDecimalSchema,
            close: marketDecimalSchema,
            change: marketDecimalSchema,
            changePercent: marketDecimalSchema,
            volume: marketDecimalSchema.nullable(),
            amountCny: marketDecimalSchema.nullable(),
            previousClose: marketDecimalSchema,
            amplitudePercent: marketDecimalSchema,
            turnoverPercent: marketDecimalSchema.nullable(),
            isFinal: z.literal(true),
          })
          .strict(),
      )
      .min(1)
      .max(1_000),
    nextCursor: z.string().min(1).max(2_048).nullable(),
  })
  .strict()
  // 页面必须只包含一个已结束周期，并使用与周期一致的写时方法学。
  .superRefine((value, context) => {
    const isDaily = value.period === '1d';
    const expectedMethodology = isDaily
      ? 'source-reported-daily-bar'
      : 'calendar-bounded-ohlcv-aggregation';
    const expectedStatus = isDaily ? 'source_reported' : 'platform_derived';
    const expectedPreviousClose = isDaily
      ? 'sw-previous-close-from-close-change'
      : 'period-opening-previous-close-from-daily';
    if (
      value.methodology.id !== expectedMethodology ||
      value.methodology.status !== expectedStatus ||
      value.methodology.previousClose.id !== expectedPreviousClose
    ) {
      context.addIssue({
        code: 'custom',
        path: ['methodology'],
        message: 'SW bar methodology must agree with the materialized period',
      });
    }
    for (const [index, item] of value.items.entries()) {
      if (
        item.period !== value.period ||
        item.periodStart > item.periodEnd ||
        (isDaily && item.periodStart !== item.periodEnd)
      ) {
        context.addIssue({
          code: 'custom',
          path: ['items', index],
          message: 'SW bar item period boundaries must agree with the page period',
        });
      }
    }
  });

/** 严格校验申万正式成员页，不把抓取观察区间冒充正式调样日期。 */
export const swIndustryConstituentPageSchema = z
  .object({
    dataVersion: z.string().uuid(),
    snapshotDate: dateOnlySchema,
    publishedAt: offsetDateTimeSchema,
    historyMode: z.literal('latest_revision_effective_interval'),
    knowledgeCutoff: offsetDateTimeSchema,
    observedAt: offsetDateTimeSchema,
    industry: z
      .object({
        code: z.string().regex(/^\d{6}\.SI$/),
        name: z.string().min(1).max(128),
        level: z.number().int().min(1).max(3),
        parentCode: z
          .string()
          .regex(/^\d{6}\.SI$/)
          .nullable(),
      })
      .strict(),
    source: marketSourceSchema,
    methodology: z
      .object({
        id: z.literal('quant-v2.sw-membership.v1'),
        version: z.literal('1'),
        status: z.literal('source_reported'),
        temporalSemantics: z.literal('latest_revision_effective_interval'),
      })
      .strict(),
    inputDataVersions: z
      .array(z.string().uuid())
      .length(2)
      .refine(hasUniqueStrings, 'SW constituent input publication versions must be unique'),
    items: z
      .array(
        swConstituentIdentitySchema
          .extend({
            inDate: dateOnlySchema.nullable(),
            outDate: dateOnlySchema.nullable(),
            isActive: z.literal(true),
          })
          .strict(),
      )
      .max(100),
    nextCursor: z.string().min(1).max(2_048).nullable(),
  })
  .strict()
  // 当前有效成员必须覆盖所选快照日，且半开有效区间自身保持严格递增。
  .superRefine((value, context) => {
    for (const [index, item] of value.items.entries()) {
      if (item.inDate !== null && item.outDate !== null && item.inDate >= item.outDate) {
        context.addIssue({
          code: 'custom',
          path: ['items', index],
          message: 'SW constituent inDate must be earlier than outDate',
        });
      }
      if (
        (item.inDate !== null && value.snapshotDate < item.inDate) ||
        (item.outDate !== null && value.snapshotDate >= item.outDate)
      ) {
        context.addIssue({
          code: 'custom',
          path: ['items', index],
          message: 'SW active constituent interval must contain snapshotDate',
        });
      }
    }
  });

/** 约束申万直报 PE 在有值和来源未报告两种状态间保持自洽。 */
const swPeMetricSchema = z.discriminatedUnion('availability', [
  z
    .object({
      value: marketDecimalSchema,
      availability: z.literal('available'),
      methodology: z
        .object({
          kind: z.literal('source_reported'),
          sourceField: z.literal('pe'),
        })
        .strict(),
    })
    .strict(),
  z
    .object({
      value: z.null(),
      availability: z.literal('source_not_reported'),
      methodology: z.null(),
    })
    .strict(),
]);

/** 约束申万直报 PB 在有值和来源未报告两种状态间保持自洽。 */
const swPbMetricSchema = z.discriminatedUnion('availability', [
  z
    .object({
      value: marketDecimalSchema,
      availability: z.literal('available'),
      methodology: z
        .object({
          kind: z.literal('source_reported'),
          sourceField: z.literal('pb'),
        })
        .strict(),
    })
    .strict(),
  z
    .object({
      value: z.null(),
      availability: z.literal('source_not_reported'),
      methodology: z.null(),
    })
    .strict(),
]);

/** 约束 Tushare `sw_daily` 未报告指标必须显式为空，不能补零或继承其他方法学。 */
const swUnavailableMetricSchema = z
  .object({
    value: z.null(),
    availability: z.literal('source_not_reported'),
    methodology: z.null(),
  })
  .strict();

/** 严格校验单个申万节点逐字段可解释的估值 publication。 */
export const swIndustryValuationSchema = z
  .object({
    dataVersion: z.string().uuid(),
    tradeDate: dateOnlySchema,
    publishedAt: offsetDateTimeSchema,
    industry: z
      .object({
        code: z.string().regex(/^\d{6}\.SI$/),
        name: z.string().min(1).max(128),
        level: z.number().int().min(1).max(3),
        parentCode: z
          .string()
          .regex(/^\d{6}\.SI$/)
          .nullable(),
      })
      .strict(),
    source: marketSourceSchema,
    methodology: z
      .object({
        id: z.literal('sw-source-reported-valuation'),
        version: z.literal('1'),
        owner: z.literal('Shenwan'),
        status: z.literal('mixed_per_field'),
      })
      .strict(),
    inputDataVersions: z
      .array(z.string().uuid())
      .length(2)
      .refine(hasUniqueStrings, 'SW valuation input publication versions must be unique'),
    valuation: z
      .object({
        pe: swPeMetricSchema,
        peTtm: swUnavailableMetricSchema,
        pb: swPbMetricSchema,
        dividendYield: swUnavailableMetricSchema,
      })
      .strict(),
    finality: z.literal('final'),
  })
  .strict();

/** 表示市场首页完整包的公开合同。 */
export type MarketOverview = z.infer<typeof marketOverviewSchema>;

/** 表示固定指数日 K 线页的公开合同。 */
export type MarketIndexBarPage = z.infer<typeof marketIndexBarPageSchema>;

/** 表示全市场证券排行页的公开合同。 */
export type MarketEquityRankingPage = z.infer<typeof marketEquityRankingPageSchema>;

/** 表示供应商订单规模方法学下的一侧证券资金流排行页。 */
export type MarketEquityMoneyFlowRankingPage = z.infer<
  typeof marketEquityMoneyFlowRankingPageSchema
>;

/** 表示交易日历与会话日程页的公开合同。 */
export type MarketCalendarPage = z.infer<typeof marketCalendarPageSchema>;

/** 表示板块强弱派生 publication 页的公开合同。 */
export type MarketSectorStrengthPage = z.infer<typeof marketSectorStrengthPageSchema>;

/** 表示东财板块来源资金流排行 publication 页的公开合同。 */
export type MarketSectorMoneyFlowRankingPage = z.infer<
  typeof marketSectorMoneyFlowRankingPageSchema
>;

/** 表示申万行业日 K 线页的公开合同。 */
export type SwIndustryBarPage = z.infer<typeof swIndustryBarPageSchema>;

/** 表示申万行业正式成分页的公开合同。 */
export type SwIndustryConstituentPage = z.infer<typeof swIndustryConstituentPageSchema>;

/** 表示一个申万节点逐字段可解释的估值 publication。 */
export type SwIndustryValuation = z.infer<typeof swIndustryValuationSchema>;

/** 判断十进制字符串是否位于包含端单位区间，供覆盖率 schema 复用。 */
function isUnitIntervalDecimal(value: string): boolean {
  return Number(value) >= 0 && Number(value) <= 1;
}

/** 在不把任意精度十进制转换为浮点数的前提下判断正、负或零。 */
function decimalSign(value: string): -1 | 0 | 1 {
  const unsignedValue = value.startsWith('-') ? value.slice(1) : value;
  if (/^0(?:\.0+)?$/.test(unsignedValue)) return 0;
  return value.startsWith('-') ? -1 : 1;
}

/** 判断版本列表是否没有重复值，避免 composite lineage 重复计数。 */
function hasUniqueStrings(values: readonly string[]): boolean {
  return new Set(values).size === values.length;
}

/** 判断四项指数列表是否恰好覆盖每个冻结稳定身份一次。 */
function hasAllPrimaryIndexIds(
  indices: readonly { indexId: z.infer<typeof primaryMarketIndexIdSchema> }[],
): boolean {
  const identities = new Set<string>();
  for (const index of indices) identities.add(index.indexId);
  return identities.size === 4;
}
