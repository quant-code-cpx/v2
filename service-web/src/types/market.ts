import { z } from "zod";

/** 约束跨服务传输的十进制字符串，避免金额、点位和百分比经过 JSON number 丢失精度。 */
export const marketDecimalSchema = z
  .string()
  .regex(/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/, "必须是规范十进制字符串");

/** 约束市场数据采用带时区偏移的观测或发布时间。 */
const offsetDateTimeSchema = z.iso.datetime({ offset: true });

/** 约束交易日等业务日期，不允许时间戳混入日历维度。 */
const dateOnlySchema = z.iso.date();

/** 约束消费者可见的游标长度，游标内容只能由服务端解释。 */
const cursorSchema = z.string().min(1).max(2_048).nullable();

/** 冻结市场首页四个主要指数的供应商中立身份。 */
const primaryMarketIndexIdSchema = z.enum([
  "sse-composite",
  "szse-component",
  "csi-300",
  "chinext",
]);

/** 验证四个主要指数在完整包中各出现且仅出现一次。 */
function hasAllPrimaryIndexIds(items: ReadonlyArray<{ indexId: string }>): boolean {
  return (
    new Set(
      items.map(
        /** 只投影稳定指数身份以执行唯一性检查。 */
        (item) => item.indexId,
      ),
    ).size === primaryMarketIndexIdSchema.options.length
  );
}

/** 验证 composite publication 的输入版本集合不包含重复组件。 */
function hasUniqueStrings(values: readonly string[]): boolean {
  return new Set(values).size === values.length;
}

/** 描述一项可审计的 Tushare 来源绑定，不暴露 token 或 raw 对象地址。 */
export const marketSourceSchema = z
  .object({
    provider: z.literal("tushare-pro"),
    upstreamSource: z.string().min(1).max(128),
    sourceDataset: z.string().min(1).max(128),
    observedAt: offsetDateTimeSchema,
    adapterVersion: z.string().min(1).max(64),
    schemaFingerprint: z.string().regex(/^[a-f0-9]{64}$/),
  })
  .strict();

/** 描述完整包中直接来自外部数据集的组件来源绑定。 */
const marketExternalSourceBindingSchema = marketSourceSchema
  .extend({
    role: z.literal("external"),
    component: z.string().min(1).max(128),
  })
  .strict();

/** 描述完整包中由平台方法学派生的组件来源绑定，不冒充外部供应商事实。 */
const marketDerivedSourceBindingSchema = z
  .object({
    role: z.literal("derived"),
    component: z.string().min(1).max(128),
    provider: z.literal("quant-v2-derivation"),
    upstreamSource: z.string().min(1).max(128),
    sourceDataset: z.string().min(1).max(128),
    observedAt: offsetDateTimeSchema,
    adapterVersion: z.string().min(1).max(64),
    schemaFingerprint: z.string().regex(/^[a-f0-9]{64}$/),
    methodology: z
      .object({
        id: z.string().min(1).max(128),
        version: z.string().min(1).max(64),
        status: z.literal("platform_derived"),
      })
      .strict(),
  })
  .strict();

/** 区分外部来源和平台派生，供完整包质量血缘逐组件展示。 */
const marketSourceBindingSchema = z.discriminatedUnion("role", [
  marketExternalSourceBindingSchema,
  marketDerivedSourceBindingSchema,
]);

/** 约束 A 股公开证券身份，浏览器不得按代码前缀猜测交易所。 */
export const marketEquityIdentitySchema = z
  .object({
    exchange: z.enum(["SSE", "SZSE", "BSE"]),
    symbol: z.string().regex(/^\d{6}$/),
    name: z.string().min(1).max(128),
  })
  .strict();

/** 约束首页市场范围内的沪深 A 股身份，明确排除北交所。 */
const marketSseSzseIdentitySchema = z
  .object({
    exchange: z.enum(["SSE", "SZSE"]),
    symbol: z.string().regex(/^\d{6}$/),
    name: z.string().min(1).max(128),
  })
  .strict();

/** 描述首页或独立排行中的证券 EOD 指标。 */
export const marketEquityRankItemSchema = marketSseSzseIdentitySchema
  .extend({
    rank: z.number().int().min(1),
    close: marketDecimalSchema,
    changePercent: marketDecimalSchema,
    amountCny: marketDecimalSchema,
    turnoverPercent: marketDecimalSchema.nullable(),
  })
  .strict();

/** 描述供应商订单规模方法学下的一条证券资金流排名。 */
const marketMoneyFlowRankItemSchema = marketSseSzseIdentitySchema
  .extend({
    rank: z.number().int().min(1),
    netAmountCny: marketDecimalSchema,
    buyLargeAmountCny: marketDecimalSchema.nullable(),
    sellLargeAmountCny: marketDecimalSchema.nullable(),
    changePercent: marketDecimalSchema.nullable(),
  })
  .strict();

/** 校验首页股票资金流双向榜，并保持流入严格为正、流出严格为负。 */
export const marketEquityMoneyFlowRankingsSchema = z
  .object({
    source: marketSourceSchema,
    methodologyId: z.literal("tushare-order-size-flow"),
    methodologyVersion: z.literal("1"),
    universe: z.literal("CN-A-SSE-SZSE-TRADED"),
    coverage: marketDecimalSchema.refine(
      /** 资金流覆盖率只能位于闭区间零到一。 */
      (value) => Number(value) >= 0 && Number(value) <= 1,
      "覆盖率必须位于零到一之间",
    ),
    inflow: z.array(marketMoneyFlowRankItemSchema).max(50),
    outflow: z.array(marketMoneyFlowRankItemSchema).max(50),
  })
  .strict()
  .superRefine(
    /** 双向榜的符号是不允许供应商记录或排序逻辑翻转的业务不变量。 */
    (value, context) => {
      value.inflow.forEach(
        /** 流入榜只能接收严格正净额，避免零值或反向记录进入榜单。 */
        (item, index) => {
          if (Number(item.netAmountCny) <= 0) {
            context.addIssue({
              code: "custom",
              path: ["inflow", index, "netAmountCny"],
              message: "流入榜净额必须严格大于零",
            });
          }
        },
      );
      value.outflow.forEach(
        /** 流出榜只能接收严格负净额，避免供应商方向语义被静默翻转。 */
        (item, index) => {
          if (Number(item.netAmountCny) >= 0) {
            context.addIssue({
              code: "custom",
              path: ["outflow", index, "netAmountCny"],
              message: "流出榜净额必须严格小于零",
            });
          }
        },
      );
    },
  );

/** 描述同一板块 publication 内的一条强弱排名。 */
const marketSectorRankItemSchema = z
  .object({
    rank: z.number().int().min(1),
    sectorCode: z.string().min(1).max(64),
    name: z.string().min(1).max(128),
    changePercent: marketDecimalSchema,
    turnoverPercent: marketDecimalSchema.nullable(),
    amountCny: marketDecimalSchema.nullable(),
    leadingEquity: marketSseSzseIdentitySchema
      .extend({ changePercent: marketDecimalSchema })
      .strict()
      .nullable(),
    validSamples: z.number().int().min(1),
  })
  .strict();

/** 描述证据化市场关注信号，禁止返回无阈值依据的自然语言判断。 */
const marketAttentionSignalSchema = z
  .object({
    signalId: z.string().min(1).max(128),
    ruleId: z.string().min(1).max(128),
    rulesVersion: z.literal("1"),
    severity: z.enum(["info", "warning"]),
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
    status: z.literal("passed"),
    actual: z.string().max(256),
    expected: z.string().max(256),
  })
  .strict();

/** 严格校验市场首页原子完整包，任何缺项都视为合同漂移。 */
export const marketOverviewSchema = z
  .object({
    dataVersion: z.string().uuid(),
    tradeDate: dateOnlySchema,
    publishedAt: offsetDateTimeSchema,
    finality: z.literal("final"),
    status: z
      .object({
        marketState: z.enum(["pre_open", "trading", "lunch_break", "closed", "non_trading_day"]),
        marketStateAsOf: offsetDateTimeSchema,
        marketStateMethodology: z.literal("calendar_schedule_derived"),
        eodEligibilityScheduleVersion: z.literal("cn-a-eod-eligibility-2026-v1"),
        freshness: z.enum(["current", "stale"]),
        latestEligibleTradeDate: dateOnlySchema,
        latestAttemptedTradeDate: dateOnlySchema.nullable(),
        lagTradingDays: z.number().int().nonnegative(),
        freshnessReason: z.enum([
          "latest_eligible_complete",
          "latest_eligible_bundle_incomplete",
          "latest_eligible_bundle_unavailable",
          "publication_rollback",
          "historical_snapshot",
        ]),
        quality: z.literal("passed"),
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
            volumeUnit: z.literal("lot"),
            amountCny: marketDecimalSchema.nullable(),
            source: marketSourceSchema,
          })
          .strict(),
      )
      .length(4)
      .refine(hasAllPrimaryIndexIds, "四个主要指数身份必须各出现且仅出现一次"),
    turnover: z
      .object({
        label: z.literal("沪深 A 股成交额"),
        universe: z.literal("CN-A-SSE-SZSE"),
        methodologyId: z.literal("sum-tushare-daily-a-share-amount-cny-v1"),
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
    equityMoneyFlowRankings: marketEquityMoneyFlowRankingsSchema,
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
            strongest: z.array(marketSectorRankItemSchema).max(50),
            weakest: z.array(marketSectorRankItemSchema).max(50),
          })
          .strict(),
        eastmoneyConcept: z
          .object({
            strongest: z.array(marketSectorRankItemSchema).max(50),
            weakest: z.array(marketSectorRankItemSchema).max(50),
          })
          .strict(),
      })
      .strict(),
    attentionSignals: z.array(marketAttentionSignalSchema).max(100),
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
  .superRefine(
    /** 完整包仅在全部组件通过时有效，Web 再次防御错误服务端投影。 */
    (value, context) => {
      if (value.quality.componentCount !== value.quality.passedCount) {
        context.addIssue({
          code: "custom",
          path: ["quality", "passedCount"],
          message: "完整包的所有组件必须通过质量门",
        });
      }
      if (value.quality.componentCount !== value.quality.sourceBindings.length) {
        context.addIssue({
          code: "custom",
          path: ["quality", "sourceBindings"],
          message: "完整包每个组件必须恰有一个来源绑定",
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
          code: "custom",
          path: ["breadth", "eligible"],
          message: "市场宽度分类总数必须等于合格证券数",
        });
      }
      if (
        value.status.freshness === "current" &&
        (value.status.lagTradingDays !== 0 ||
          value.status.freshnessReason !== "latest_eligible_complete")
      ) {
        context.addIssue({
          code: "custom",
          path: ["status", "freshness"],
          message: "current 必须对应最新合格交易日完整包",
        });
      }
      if (
        value.status.freshness === "stale" &&
        value.status.freshnessReason === "latest_eligible_complete"
      ) {
        context.addIssue({
          code: "custom",
          path: ["status", "freshness"],
          message: "stale 必须携带非 complete 原因",
        });
      }
      if (
        value.status.freshness === "stale" &&
        (value.status.freshnessReason === "latest_eligible_bundle_incomplete" ||
          value.status.freshnessReason === "latest_eligible_bundle_unavailable") &&
        value.status.lagTradingDays === 0
      ) {
        context.addIssue({
          code: "custom",
          path: ["status", "lagTradingDays"],
          message: "缺失或不完整的最新交易日必须产生正滞后",
        });
      }
      if (
        value.status.freshnessReason === "historical_snapshot" &&
        (value.status.freshness !== "stale" ||
          value.status.marketState !== "closed" ||
          value.status.lagTradingDays !== 0 ||
          value.status.latestEligibleTradeDate !== value.tradeDate ||
          value.status.latestAttemptedTradeDate !== null)
      ) {
        context.addIssue({
          code: "custom",
          path: ["status", "freshnessReason"],
          message: "历史快照必须冻结为所选交易日收盘状态且不表达数据延迟",
        });
      }
    },
  );

/** 描述指数日 K 线中的一个来源直报交易日。 */
export const marketDailyBarSchema = z
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
    finality: z.literal("final"),
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
    period: z.literal("1d"),
    source: marketSourceSchema,
    volumeUnit: z.literal("lot"),
    inputDataVersions: z
      .array(z.string().uuid())
      .min(1)
      .max(7_500)
      .refine(hasUniqueStrings, "指数 K 线输入 publication 版本必须唯一"),
    items: z.array(marketDailyBarSchema).max(1_000),
    nextCursor: cursorSchema,
  })
  .strict();

/** 严格校验冻结全市场横截面派生的证券排行页。 */
export const marketEquityRankingPageSchema = z
  .object({
    dataVersion: z.string().uuid(),
    tradeDate: dateOnlySchema,
    publishedAt: offsetDateTimeSchema,
    source: marketSourceSchema,
    metric: z.enum(["changePercent", "amountCny", "turnoverPercent"]),
    order: z.enum(["asc", "desc"]),
    universe: z.literal("CN-A-SSE-SZSE-ELIGIBLE"),
    coverage: marketDecimalSchema.refine(
      /** 股票排行覆盖率只能位于闭区间零到一。 */
      (value) => Number(value) >= 0 && Number(value) <= 1,
      "覆盖率必须位于零到一之间",
    ),
    finality: z.literal("final"),
    quality: z
      .object({
        status: z.literal("passed"),
        universeVersion: z.string().min(1).max(128),
        checks: z.array(marketQualityCheckSchema).min(1).max(128),
      })
      .strict(),
    items: z.array(marketEquityRankItemSchema).max(50),
    nextCursor: cursorSchema,
  })
  .strict();

/** 严格校验同一分类体系与同一 publication 的板块强弱页。 */
export const marketSectorStrengthPageSchema = z
  .object({
    dataVersion: z.string().uuid(),
    tradeDate: dateOnlySchema,
    publishedAt: offsetDateTimeSchema,
    scheme: z.enum(["eastmoney.industry", "eastmoney.concept"]),
    window: z.union([z.literal(1), z.literal(5), z.literal(20)]),
    order: z.enum(["asc", "desc"]),
    methodologyVersion: z.string().min(1).max(64),
    source: marketSourceSchema,
    inputDataVersions: z.array(z.string().uuid()).min(1).max(252),
    quality: z
      .object({
        status: z.literal("passed"),
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
            coverage: z.literal("1"),
          })
          .strict(),
      )
      .min(1)
      .max(100),
    nextCursor: cursorSchema,
  })
  .strict()
  .superRefine(
    /** 校验强弱窗口、输入 publication 与返回样本保持完整一致。 */
    (value, context) => {
      if (value.inputDataVersions.length !== value.window) {
        context.addIssue({
          code: "custom",
          path: ["inputDataVersions"],
          message: "强弱输入 publication 数必须等于请求交易日窗口",
        });
      }
      if (value.quality.validUniverseCount < value.items.length) {
        context.addIssue({
          code: "custom",
          path: ["quality", "validUniverseCount"],
          message: "有效板块范围必须覆盖所有返回排行项",
        });
      }
      value.items.forEach(
        /** 校验每个排行项都具有完整窗口样本。 */
        (item, index) => {
          if (item.validSamples !== value.window) {
            context.addIssue({
              code: "custom",
              path: ["items", index, "validSamples"],
              message: "强弱有效样本数必须等于请求交易日窗口",
            });
          }
        },
      );
    },
  );

/** 校验一个东财分类体系的来源资金流排行 publication。 */
export const marketSectorMoneyFlowRankingPageSchema = z
  .object({
    dataVersion: z.string().uuid(),
    tradeDate: dateOnlySchema,
    publishedAt: offsetDateTimeSchema,
    scheme: z.enum(["eastmoney.industry", "eastmoney.concept"]),
    order: z.enum(["asc", "desc"]),
    source: marketSourceSchema,
    methodology: z
      .object({
        id: z.literal("eastmoney-sector-flow-dc"),
        version: z.literal("unknown"),
        semanticFamily: z.literal("trade_direction_flow"),
        status: z.literal("source_reported"),
        rankingBasis: z.literal("canonical_net_amount"),
      })
      .strict(),
    coverage: z.literal("1"),
    finality: z.literal("final"),
    quality: z
      .object({
        status: z.literal("passed"),
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
    nextCursor: cursorSchema,
  })
  .strict()
  .superRefine(
    /** 校验板块资金流有效范围覆盖所有返回排行项。 */
    (value, context) => {
      if (value.quality.validUniverseCount < value.items.length) {
        context.addIssue({
          code: "custom",
          path: ["quality", "validUniverseCount"],
          message: "有效板块范围必须覆盖所有资金流排行项",
        });
      }
    },
  );

/** 约束东财行业与概念两套不可混排的分类体系。 */
export const marketSectorSchemes = ["eastmoney.industry", "eastmoney.concept"] as const;

/** 约束板块横截面允许的稳定排序字段。 */
export const marketSectorEodSorts = [
  "changePercent",
  "turnoverPercent",
  "marketValue",
  "latestValue",
  "advancers",
  "decliners",
  "leaderChangePercent",
  "code",
] as const;

/** 校验公开板块目录身份，不包含同步服务内部 UUID。 */
export const marketSectorSchema = z
  .object({
    scheme: z.enum(marketSectorSchemes),
    code: z.string().min(1).max(64),
    name: z.string().min(1).max(200),
    dataVersion: z.string().uuid(),
    publishedAt: z.string().min(1),
  })
  .strict();

/** 校验公开板块目录分页。 */
export const marketSectorPageSchema = z
  .object({
    items: z.array(marketSectorSchema).max(100),
    nextCursor: z.string().max(1_024).nullable(),
    dataVersion: z.string().uuid(),
    publishedAt: z.string().min(1),
  })
  .strict();

/** 校验板块来源直报的独立物理周期 K 线。 */
export const marketSectorBarSchema = z
  .object({
    periodEnd: dateOnlySchema,
    open: marketDecimalSchema,
    high: marketDecimalSchema,
    low: marketDecimalSchema,
    close: marketDecimalSchema,
    volumeValue: marketDecimalSchema.nullable(),
    volumeUnit: z.literal("provider_native"),
    amountCny: marketDecimalSchema.nullable(),
    amplitudePercent: marketDecimalSchema.nullable(),
    changePercent: marketDecimalSchema.nullable(),
    changeAmount: marketDecimalSchema.nullable(),
    turnoverPercent: marketDecimalSchema.nullable(),
    isFinal: z.literal(true),
  })
  .strict();

/** 校验板块日、周或月 K 线分页。 */
export const marketSectorBarPageSchema = z
  .object({
    sector: marketSectorSchema,
    period: z.enum(["1d", "1w", "1mo"]),
    dataVersion: z.string().uuid(),
    publishedAt: z.string().min(1),
    items: z.array(marketSectorBarSchema).max(1_000),
    nextCursor: z.string().max(1_024).nullable(),
  })
  .strict();

/** 校验公开板块 EOD 快照共有元数据。 */
const marketSectorEodMetadataSchema = z
  .object({
    scheme: z.enum(marketSectorSchemes),
    tradeDate: dateOnlySchema,
    sourceCutoffAt: offsetDateTimeSchema,
    observedAt: offsetDateTimeSchema,
    finality: z.literal("post_close_observation"),
    qualityStatus: z.enum(["passed", "warned"]),
    dataVersion: z.string().uuid(),
    publishedAt: offsetDateTimeSchema,
    inputDataVersions: z
      .array(z.string().uuid())
      .length(2)
      .refine(hasUniqueStrings, "板块 EOD composite 的两个输入版本必须唯一"),
  })
  .strict();

/** 校验公开板块 EOD 指标；板块点位保留来源单位，市值统一为人民币元。 */
const marketSectorEodValueSchema = z
  .object({
    code: z.string().min(1).max(64),
    name: z.string().min(1).max(200),
    latestValue: marketDecimalSchema.nullable(),
    latestValueUnit: z.literal("provider_native"),
    changeValue: marketDecimalSchema.nullable(),
    changePercent: marketDecimalSchema.nullable(),
    marketValue: marketDecimalSchema.nullable(),
    marketValueUnit: z.literal("CNY"),
    turnoverPercent: marketDecimalSchema.nullable(),
    advancers: z.number().int().nonnegative().nullable(),
    decliners: z.number().int().nonnegative().nullable(),
    leaderName: z.string().max(200).nullable(),
    leaderChangePercent: marketDecimalSchema.nullable(),
  })
  .strict();

/** 校验同一 EOD publication 内的板块排行页。 */
export const marketSectorEodPageSchema = marketSectorEodMetadataSchema
  .extend({
    sort: z.enum(marketSectorEodSorts),
    order: z.enum(["asc", "desc"]),
    items: z
      .array(
        marketSectorEodValueSchema
          .extend({
            scheme: z.enum(marketSectorSchemes),
            rank: z.number().int().positive().nullable(),
            position: z.number().int().positive(),
          })
          .strict(),
      )
      .max(500),
    nextCursor: z.string().max(1_024).nullable(),
  })
  .strict();

/** 校验单板块 EOD 快照。 */
export const marketSectorEodResourceSchema = marketSectorEodMetadataSchema
  .extend(marketSectorEodValueSchema.shape)
  .strict();

/** 校验成分观测绑定的 publication 与覆盖质量。 */
const marketSectorMembershipReleaseSchema = z
  .object({
    requestedAsOf: offsetDateTimeSchema.nullable(),
    resolvedAsOf: offsetDateTimeSchema,
    coverageStart: offsetDateTimeSchema,
    membershipSemantics: z.literal("observed"),
    qualityStatus: z.enum(["passed", "warned"]),
    identityCoveragePercent: z.literal("100"),
    excludedIdentityCount: z.literal(0),
    carriedForwardSectorCount: z.number().int().nonnegative(),
    dataVersion: z.string().uuid(),
    publishedAt: offsetDateTimeSchema,
  })
  .strict();

/** 校验一个板块的 verified 当前观察成分页。 */
export const marketSectorConstituentPageSchema = z
  .object({
    sector: z
      .object({
        scheme: z.enum(marketSectorSchemes),
        code: z.string().min(1).max(64),
        name: z.string().min(1).max(200),
      })
      .strict(),
    release: marketSectorMembershipReleaseSchema,
    snapshotObservedAt: offsetDateTimeSchema,
    carriedForward: z.boolean(),
    items: z
      .array(
        marketEquityIdentitySchema
          .extend({
            listingStatus: z.enum(["LISTED", "SUSPENDED", "DELISTED"]),
            observedFrom: offsetDateTimeSchema,
            observedTo: offsetDateTimeSchema.nullable(),
          })
          .strict(),
      )
      .max(500),
    nextCursor: z.string().max(1_024).nullable(),
  })
  .strict();

/** 校验申万分类或估值读取绑定的不可变 publication。 */
export const swIndustryReleaseSchema = z
  .object({
    snapshotDate: dateOnlySchema,
    dataVersion: z.string().uuid(),
    publishedAt: offsetDateTimeSchema,
    qualityStatus: z.enum(["passed", "warned"]),
    rowCount: z.number().int().positive(),
    methodology: z
      .object({
        code: z.string().min(1).max(80),
        version: z.number().int().positive(),
        status: z.literal("source_reported"),
        upstreamSource: z.string().min(1).max(120),
        semanticSpecSha256: z.string().regex(/^[0-9a-f]{64}$/),
      })
      .strict(),
  })
  .strict();

/** 校验申万节点、直接父级和当前知识修订。 */
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

/** 校验申万 taxonomy 分页。 */
export const swIndustryPageSchema = z
  .object({
    scheme: z.literal("sw.industry"),
    release: swIndustryReleaseSchema,
    items: z.array(swIndustryNodeSchema).max(500),
    nextCursor: z.string().max(1_024).nullable(),
  })
  .strict();

/** 校验一个申万节点及其冻结发布中的根到直接父级闭包。 */
export const swIndustryResourceSchema = z
  .object({
    scheme: z.literal("sw.industry"),
    release: swIndustryReleaseSchema,
    industry: swIndustryNodeSchema,
    ancestors: z.array(swIndustryNodeSchema).max(2),
  })
  .strict();

/** 校验旧版申万估值分页中的供应商观察行。 */
const swIndustryValuationItemSchema = swIndustryNodeSchema
  .omit({ revision: true })
  .extend({
    snapshotDate: dateOnlySchema,
    staticPe: marketDecimalSchema.nullable(),
    ttmPe: marketDecimalSchema.nullable(),
    pb: marketDecimalSchema.nullable(),
    dividendYieldRatio: marketDecimalSchema.nullable(),
    finality: z.literal("PROVIDER_OBSERVATION"),
    valuationRevision: z.number().int().positive(),
  })
  .strict();

/** 校验申万估值分页。 */
export const swIndustryValuationPageSchema = z
  .object({
    scheme: z.literal("sw.industry"),
    release: swIndustryReleaseSchema,
    items: z.array(swIndustryValuationItemSchema).max(500),
    nextCursor: z.string().max(1_024).nullable(),
  })
  .strict();

/** 校验申万行业同步期已物化 K 线，并保留周期聚合与昨收方法学。 */
export const swIndustryBarPageSchema = z
  .object({
    dataVersion: z.string().uuid(),
    publishedAt: offsetDateTimeSchema,
    industry: z
      .object({
        code: z.string().regex(/^[0-9]{6}\.SI$/),
        name: z.string().min(1).max(128),
        level: z.number().int().min(1).max(3),
        parentCode: z
          .string()
          .regex(/^[0-9]{6}\.SI$/)
          .nullable(),
      })
      .strict(),
    period: z.enum(["1d", "1w", "1mo"]),
    source: marketSourceSchema,
    volumeUnit: z.literal("provider_native"),
    methodology: z
      .object({
        id: z.enum(["source-reported-daily-bar", "calendar-bounded-ohlcv-aggregation"]),
        version: z.literal("1"),
        status: z.enum(["source_reported", "platform_derived"]),
        inputDataset: z.literal("sw.market-data"),
        previousClose: z.discriminatedUnion("id", [
          z
            .object({
              kind: z.literal("derived"),
              id: z.literal("sw-previous-close-from-close-change"),
              version: z.literal("1"),
              inputs: z.tuple([z.literal("close"), z.literal("change")]),
            })
            .strict(),
          z
            .object({
              kind: z.literal("derived"),
              id: z.literal("period-opening-previous-close-from-daily"),
              version: z.literal("1"),
              inputs: z.tuple([z.literal("daily.previousClose")]),
            })
            .strict(),
        ]),
      })
      .strict(),
    inputDataVersions: z
      .array(z.string().uuid())
      .min(1)
      .max(7_500)
      .refine(hasUniqueStrings, "申万 K 线输入 publication 版本必须唯一"),
    finality: z.literal("final"),
    items: z
      .array(
        z
          .object({
            period: z.enum(["1d", "1w", "1mo"]),
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
    nextCursor: cursorSchema,
  })
  .strict()
  .superRefine(
    /** 校验页面周期、写时方法学与每根已结束周期保持一致。 */
    (value, context) => {
      const isDaily = value.period === "1d";
      const expectedMethodology = isDaily
        ? "source-reported-daily-bar"
        : "calendar-bounded-ohlcv-aggregation";
      const expectedStatus = isDaily ? "source_reported" : "platform_derived";
      const expectedPreviousClose = isDaily
        ? "sw-previous-close-from-close-change"
        : "period-opening-previous-close-from-daily";

      if (
        value.methodology.id !== expectedMethodology ||
        value.methodology.status !== expectedStatus ||
        value.methodology.previousClose.id !== expectedPreviousClose
      ) {
        context.addIssue({
          code: "custom",
          path: ["methodology"],
          message: "申万 K 线方法学必须与已物化周期一致",
        });
      }

      value.items.forEach(
        /** 校验每根记录归属当前周期，且日线起止日必须相同。 */
        (item, index) => {
          if (
            item.period !== value.period ||
            item.periodStart > item.periodEnd ||
            (isDaily && item.periodStart !== item.periodEnd)
          ) {
            context.addIssue({
              code: "custom",
              path: ["items", index],
              message: "申万 K 线周期边界必须与页面周期一致",
            });
          }
        },
      );
    },
  );

/** 校验申万正式成员页，不把抓取观察区间冒充正式调样日期。 */
export const swIndustryConstituentPageSchema = z
  .object({
    dataVersion: z.string().uuid(),
    snapshotDate: dateOnlySchema,
    publishedAt: offsetDateTimeSchema,
    historyMode: z.literal("latest_revision_effective_interval"),
    knowledgeCutoff: offsetDateTimeSchema,
    observedAt: offsetDateTimeSchema,
    source: marketSourceSchema,
    inputDataVersions: z
      .array(z.string().uuid())
      .length(2)
      .refine(hasUniqueStrings, "申万成员 composite 的两个输入版本必须唯一"),
    methodology: z
      .object({
        id: z.literal("quant-v2.sw-membership.v1"),
        version: z.literal("1"),
        status: z.literal("source_reported"),
        temporalSemantics: z.literal("latest_revision_effective_interval"),
      })
      .strict(),
    industry: z
      .object({
        code: z.string().regex(/^[0-9]{6}\.SI$/),
        name: z.string().min(1).max(128),
        level: z.number().int().min(1).max(3),
        parentCode: z
          .string()
          .regex(/^[0-9]{6}\.SI$/)
          .nullable(),
      })
      .strict(),
    items: z
      .array(
        marketEquityIdentitySchema
          .extend({
            inDate: dateOnlySchema.nullable(),
            outDate: dateOnlySchema.nullable(),
            isActive: z.literal(true),
          })
          .strict(),
      )
      .max(100),
    nextCursor: cursorSchema,
  })
  .strict()
  .superRefine(
    /** 校验最新修订有效区间真实覆盖请求快照日，不把观察时间冒充调样时间。 */
    (value, context) => {
      value.items.forEach(
        /** 对开放边界分别校验；双边存在时还必须构成严格非空半开区间。 */
        (item, index) => {
          if (item.inDate !== null && item.inDate > value.snapshotDate) {
            context.addIssue({
              code: "custom",
              path: ["items", index, "inDate"],
              message: "成分纳入日不得晚于快照日",
            });
          }
          if (item.outDate !== null && item.outDate <= value.snapshotDate) {
            context.addIssue({
              code: "custom",
              path: ["items", index, "outDate"],
              message: "成分移出日必须晚于快照日",
            });
          }
          if (item.inDate !== null && item.outDate !== null && item.inDate >= item.outDate) {
            context.addIssue({
              code: "custom",
              path: ["items", index],
              message: "成分纳入日必须早于移出日",
            });
          }
        },
      );
    },
  );

/** 校验来源直报 PE 指标的逐字段可用性。 */
const swPeMetricSchema = z.discriminatedUnion("availability", [
  z
    .object({
      value: marketDecimalSchema,
      availability: z.literal("available"),
      methodology: z
        .object({
          kind: z.literal("source_reported"),
          sourceField: z.literal("pe"),
        })
        .strict(),
    })
    .strict(),
  z
    .object({
      value: z.null(),
      availability: z.literal("source_not_reported"),
      methodology: z.null(),
    })
    .strict(),
]);

/** 校验来源直报 PB 指标的逐字段可用性。 */
const swPbMetricSchema = z.discriminatedUnion("availability", [
  z
    .object({
      value: marketDecimalSchema,
      availability: z.literal("available"),
      methodology: z
        .object({
          kind: z.literal("source_reported"),
          sourceField: z.literal("pb"),
        })
        .strict(),
    })
    .strict(),
  z
    .object({
      value: z.null(),
      availability: z.literal("source_not_reported"),
      methodology: z.null(),
    })
    .strict(),
]);

/** 约束供应商未报告指标必须显式为空，禁止补零或套用其他方法学。 */
const swUnavailableMetricSchema = z
  .object({
    value: z.null(),
    availability: z.literal("source_not_reported"),
    methodology: z.null(),
  })
  .strict();

/** 校验一个申万节点逐字段可解释的估值 publication。 */
export const swIndustryValuationSchema = z
  .object({
    dataVersion: z.string().uuid(),
    tradeDate: dateOnlySchema,
    publishedAt: offsetDateTimeSchema,
    industry: z
      .object({
        code: z.string().regex(/^[0-9]{6}\.SI$/),
        name: z.string().min(1).max(128),
        level: z.number().int().min(1).max(3),
        parentCode: z
          .string()
          .regex(/^[0-9]{6}\.SI$/)
          .nullable(),
      })
      .strict(),
    source: marketSourceSchema,
    inputDataVersions: z
      .array(z.string().uuid())
      .length(2)
      .refine(hasUniqueStrings, "申万估值 composite 的两个输入版本必须唯一"),
    methodology: z
      .object({
        id: z.literal("sw-source-reported-valuation"),
        version: z.literal("1"),
        owner: z.literal("Shenwan"),
        status: z.literal("mixed_per_field"),
      })
      .strict(),
    valuation: z
      .object({
        pe: swPeMetricSchema,
        peTtm: swUnavailableMetricSchema,
        pb: swPbMetricSchema,
        dividendYield: swUnavailableMetricSchema,
      })
      .strict(),
    finality: z.literal("final"),
  })
  .strict();

/** 表示市场首页原子完整包。 */
export type MarketOverview = z.infer<typeof marketOverviewSchema>;

/** 表示固定指数日 K 线页。 */
export type MarketIndexBarPage = z.infer<typeof marketIndexBarPageSchema>;

/** 表示全市场证券排行页。 */
export type MarketEquityRankingPage = z.infer<typeof marketEquityRankingPageSchema>;

/** 表示板块分类体系枚举。 */
export type MarketSectorScheme = (typeof marketSectorSchemes)[number];

/** 表示板块 EOD 排序枚举。 */
export type MarketSectorEodSort = (typeof marketSectorEodSorts)[number];

/** 表示板块目录页。 */
export type MarketSectorPage = z.infer<typeof marketSectorPageSchema>;

/** 表示板块 EOD 横截面排行页。 */
export type MarketSectorEodPage = z.infer<typeof marketSectorEodPageSchema>;

/** 表示单板块 EOD 快照。 */
export type MarketSectorEodResource = z.infer<typeof marketSectorEodResourceSchema>;

/** 表示板块 K 线页。 */
export type MarketSectorBarPage = z.infer<typeof marketSectorBarPageSchema>;

/** 表示板块当前观察成分页。 */
export type MarketSectorConstituentPage = z.infer<typeof marketSectorConstituentPageSchema>;

/** 表示板块强弱 publication 页。 */
export type MarketSectorStrengthPage = z.infer<typeof marketSectorStrengthPageSchema>;

/** 表示东财板块来源资金流排行 publication 页。 */
export type MarketSectorMoneyFlowRankingPage = z.infer<
  typeof marketSectorMoneyFlowRankingPageSchema
>;

/** 表示申万 taxonomy 分页。 */
export type SwIndustryPage = z.infer<typeof swIndustryPageSchema>;

/** 表示申万节点及父级闭包。 */
export type SwIndustryResource = z.infer<typeof swIndustryResourceSchema>;

/** 表示申万估值分页。 */
export type SwIndustryValuationPage = z.infer<typeof swIndustryValuationPageSchema>;

/** 表示单个申万节点逐字段可解释的估值 publication。 */
export type SwIndustryValuation = z.infer<typeof swIndustryValuationSchema>;

/** 表示申万同步期已物化的日、周或月 K 线页。 */
export type SwIndustryBarPage = z.infer<typeof swIndustryBarPageSchema>;

/** 表示申万正式成分页。 */
export type SwIndustryConstituentPage = z.infer<typeof swIndustryConstituentPageSchema>;
