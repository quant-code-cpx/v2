import { z, type RefinementCtx } from 'zod';

/** 固定一期 ETF typed dataset，未知版本必须在公开边界失败关闭。 */
export const ETF_MARKET_DATASETS = [
  'fund.etf.profile.reported',
  'fund.etf.bar.1d.reported',
  'fund.etf.nav.1d.reported',
  'fund.etf.trading_state.reported',
] as const;

/** 表示一期 ETF typed dataset 代码。 */
export type EtfMarketDataset = (typeof ETF_MARKET_DATASETS)[number];

/** 固定内部市场数据协议支持的有限过滤运算符。 */
const FILTER_OPERATORS = ['EQ', 'IN', 'GTE', 'LTE', 'RANGE', 'PREFIX', 'CONTAINS'] as const;

/** 表示内部市场数据协议支持的过滤运算符。 */
type FilterOperator = (typeof FILTER_OPERATORS)[number];

/** 约束业务日期、带偏移时间、关联标识、UUID、币种和十进制字符串。 */
const dateSchema = z.string().date();
const dateTimeSchema = z.string().datetime({ offset: true });
const requestIdSchema = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/);
const uuidSchema = z.string().uuid();
const currencySchema = z.string().regex(/^[A-Z]{3}$/);
const nonNegativeDecimalSchema = z.string().regex(/^[0-9]+(?:\.[0-9]+)?$/);

/** 表示可安全穿越服务边界的 JSON 对象，不允许顶层值携带数组或标量。 */
const jsonObjectSchema = z.record(z.string(), z.unknown());

/** 约束过滤器中的有界 JSON 标量，拒绝不安全整数和嵌套表达式。 */
const filterScalarSchema = z.union([
  z.string().max(120),
  z.number().int().min(Number.MIN_SAFE_INTEGER).max(Number.MAX_SAFE_INTEGER),
  z.boolean(),
]);

/** 定义通用 typed reader 的单个有限过滤条件。 */
const filterSchema = z
  .object({
    field: z.string().regex(/^[A-Za-z][A-Za-z0-9]*$/),
    operator: z.enum(FILTER_OPERATORS),
    values: z.array(filterScalarSchema).min(1).max(500),
  })
  .strict();

/** 定义通用 typed reader 的单个稳定排序条件。 */
const sortSchema = z
  .object({
    field: z.string().regex(/^[A-Za-z][A-Za-z0-9]*$/),
    direction: z.enum(['ASC', 'DESC']),
  })
  .strict();

/** 定义通用 typed reader 的有界不透明分页参数。 */
const pageSchema = z
  .object({
    limit: z.number().int().min(1).max(500).optional(),
    cursor: z.string().min(1).max(2_048).nullable().optional(),
  })
  .strict();

/** 定义 ETF v2 只接受的日历时间范围。 */
const etfTimeSchema = z
  .object({
    dimension: z.enum(['EFFECTIVE_AT', 'TRADE_DATE']),
    from: dateSchema,
    to: dateSchema,
    timezone: z.literal('Asia/Shanghai').optional(),
  })
  .strict();

/** 定义 ETF v2 当前视图，不允许夹带 PIT 时间。 */
const currentVisibilitySchema = z.object({ mode: z.literal('CURRENT') }).strict();

/** 定义 ETF v2 已实现的 publication 选择器，只允许当前或精确数据版本。 */
const etfSelectionSchema = z
  .object({
    dataVersion: uuidSchema.optional(),
    qualityStatuses: z
      .array(z.enum(['PASSED', 'WARNED']))
      .min(1)
      .max(2),
  })
  .strict();

/** 固定 ETF v2 每个 dataset 可选择的业务字段。 */
const ETF_FIELDS: Record<EtfMarketDataset, ReadonlySet<string>> = {
  'fund.etf.profile.reported': new Set([
    'etfEntityRef',
    'exchange',
    'symbol',
    'displayName',
    'etfType',
    'managementMode',
    'managerName',
    'custodianName',
    'listedOn',
    'delistedOn',
    'listingStatus',
    'quoteCurrency',
    'navCurrency',
    'sourceTimePrecision',
  ]),
  'fund.etf.bar.1d.reported': new Set([
    'tradeDate',
    'etfEntityRef',
    'open',
    'high',
    'low',
    'close',
    'volume',
    'volumeUnit',
    'amount',
    'currency',
    'tradeStatus',
    'adjustment',
  ]),
  'fund.etf.nav.1d.reported': new Set([
    'navDate',
    'etfEntityRef',
    'navKind',
    'nav',
    'currency',
    'finality',
  ]),
  'fund.etf.trading_state.reported': new Set([
    'etfEntityRef',
    'stateDimension',
    'state',
    'effectiveFrom',
    'effectiveTo',
    'reason',
  ]),
};

/** 固定 ETF v2 每个 dataset 的过滤字段与运算符白名单。 */
const ETF_FILTERS: Record<EtfMarketDataset, Readonly<Record<string, readonly FilterOperator[]>>> = {
  'fund.etf.profile.reported': {
    etfEntityRef: ['EQ', 'IN'],
    exchange: ['EQ', 'IN'],
    symbol: ['EQ', 'PREFIX'],
    displayName: ['CONTAINS'],
    listingStatus: ['EQ', 'IN'],
  },
  'fund.etf.bar.1d.reported': {
    etfEntityRef: ['EQ', 'IN'],
  },
  'fund.etf.nav.1d.reported': {
    etfEntityRef: ['EQ', 'IN'],
    navKind: ['EQ', 'IN'],
  },
  'fund.etf.trading_state.reported': {
    etfEntityRef: ['EQ', 'IN'],
    stateDimension: ['EQ', 'IN'],
    state: ['EQ', 'IN'],
  },
};

/** 固定 ETF v2 每个 dataset 的排序字段白名单。 */
const ETF_SORT_FIELDS: Record<EtfMarketDataset, ReadonlySet<string>> = {
  'fund.etf.profile.reported': new Set(['symbol', 'displayName', 'etfEntityRef']),
  'fund.etf.bar.1d.reported': new Set(['tradeDate']),
  'fund.etf.nav.1d.reported': new Set(['navDate']),
  'fund.etf.trading_state.reported': new Set(['effectiveFrom']),
};

/** 定义公开端可转交给内部 typed reader 的单 dataset 查询请求骨架。 */
const marketDataQueryRequestBaseSchema = z
  .object({
    dataset: z
      .object({
        code: z.string().regex(/^[a-z][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)+$/),
        schemaVersion: z.number().int().positive(),
      })
      .strict(),
    businessScope: z.enum([
      'MARKET',
      'SECURITY',
      'INDEX',
      'ETF',
      'FUND',
      'CHANNEL',
      'REPORT',
      'EVENT',
      'CONTRACT',
    ]),
    time: jsonObjectSchema,
    visibility: jsonObjectSchema,
    selection: jsonObjectSchema,
    fields: z.array(z.string().min(1).max(120)).min(1).max(64),
    sort: z.array(sortSchema).min(1).max(3),
    identity: jsonObjectSchema.optional(),
    filters: z.array(filterSchema).max(20).optional(),
    page: pageSchema.optional(),
  })
  .strict();

/** 校验通用唯一性与 ETF v2 dataset-specific 查询约束。 */
function validateMarketDataQueryRequest(
  value: z.infer<typeof marketDataQueryRequestBaseSchema>,
  context: RefinementCtx,
): void {
  if (hasDuplicates(value.fields)) {
    addIssue(context, 'fields must contain unique values');
  }
  const filterFields: string[] = [];
  for (const filter of value.filters ?? []) filterFields.push(filter.field);
  if (hasDuplicates(filterFields)) {
    addIssue(context, 'filter fields must be unique');
  }
  const sortFields: string[] = [];
  for (const sort of value.sort) sortFields.push(sort.field);
  if (hasDuplicates(sortFields)) {
    addIssue(context, 'sort fields must be unique');
  }
  if (!isEtfMarketDataset(value.dataset.code)) return;
  if (value.dataset.schemaVersion === 1) return;
  if (value.dataset.schemaVersion !== 2) {
    addIssue(context, 'ETF dataset schemaVersion is unsupported');
    return;
  }
  validateEtfV2Query(value, context);
}

/** 校验 ETF v2 的业务范围、时间、publication 选择、字段、筛选、排序和分页上限。 */
function validateEtfV2Query(
  value: z.infer<typeof marketDataQueryRequestBaseSchema>,
  context: RefinementCtx,
): void {
  const dataset = value.dataset.code as EtfMarketDataset;
  if (value.businessScope !== 'ETF') {
    addIssue(context, 'ETF v2 query requires ETF businessScope');
  }
  if (value.identity !== undefined) {
    addIssue(context, 'ETF v2 query must use explicit filters instead of identity');
  }
  const time = etfTimeSchema.safeParse(value.time);
  if (!time.success) {
    addIssue(context, 'ETF v2 time is invalid');
  } else {
    validateEtfTime(dataset, time.data, context);
  }
  if (!currentVisibilitySchema.safeParse(value.visibility).success) {
    addIssue(context, 'ETF v2 visibility is invalid');
  }
  const selection = etfSelectionSchema.safeParse(value.selection);
  if (!selection.success || (selection.success && hasDuplicates(selection.data.qualityStatuses))) {
    addIssue(context, 'ETF v2 selection is invalid');
  }
  for (const field of value.fields) {
    if (!ETF_FIELDS[dataset].has(field)) addIssue(context, 'ETF v2 field is unsupported');
  }
  for (const filter of value.filters ?? []) validateEtfFilter(dataset, filter, context);
  for (const sort of value.sort) {
    if (!ETF_SORT_FIELDS[dataset].has(sort.field)) {
      addIssue(context, 'ETF v2 sort field is unsupported');
    }
  }
  validateRequiredEtfFilters(dataset, value.filters ?? [], context);
  validateEtfPage(dataset, value.page, context);
}

/** 校验 ETF v2 时间维度、目录单日快照及详情数据的 366 个自然日窗口。 */
function validateEtfTime(
  dataset: EtfMarketDataset,
  time: z.infer<typeof etfTimeSchema>,
  context: RefinementCtx,
): void {
  const expectedDimension =
    dataset === 'fund.etf.bar.1d.reported' || dataset === 'fund.etf.nav.1d.reported'
      ? 'TRADE_DATE'
      : 'EFFECTIVE_AT';
  if (time.dimension !== expectedDimension || time.from > time.to) {
    addIssue(context, 'ETF v2 time dimension or range is invalid');
  }
  if (dataset === 'fund.etf.profile.reported' && time.from !== time.to) {
    addIssue(context, 'ETF profile requires a single effective date');
  }
  if (dataset !== 'fund.etf.profile.reported' && inclusiveCalendarDays(time.from, time.to) > 366) {
    addIssue(context, 'ETF v2 detail range exceeds 366 days');
  }
}

/** 校验一个 ETF v2 过滤器的字段、运算符、值数量和业务标量。 */
function validateEtfFilter(
  dataset: EtfMarketDataset,
  filter: z.infer<typeof filterSchema>,
  context: RefinementCtx,
): void {
  const operators = ETF_FILTERS[dataset][filter.field];
  if (operators === undefined || !operators.includes(filter.operator)) {
    addIssue(context, 'ETF v2 filter is unsupported');
    return;
  }
  if (
    (filter.operator === 'EQ' ||
      filter.operator === 'GTE' ||
      filter.operator === 'LTE' ||
      filter.operator === 'PREFIX' ||
      filter.operator === 'CONTAINS') &&
    filter.values.length !== 1
  ) {
    addIssue(context, 'ETF v2 scalar filter requires exactly one value');
  }
  if (filter.operator === 'RANGE' && filter.values.length !== 2) {
    addIssue(context, 'ETF v2 range filter requires exactly two values');
  }
  if (new Set(filter.values).size !== filter.values.length) {
    addIssue(context, 'ETF v2 filter values must be unique');
  }
  validateEtfFilterValues(filter, context);
}

/** 按字段校验 ETF v2 过滤值，不从代码前缀推断场所或产品类型。 */
function validateEtfFilterValues(
  filter: z.infer<typeof filterSchema>,
  context: RefinementCtx,
): void {
  for (const value of filter.values) {
    if (typeof value !== 'string') {
      addIssue(context, 'ETF v2 filter values must be strings');
      continue;
    }
    if (filter.field === 'etfEntityRef' && !uuidSchema.safeParse(value).success) {
      addIssue(context, 'ETF entity filter must contain UUID values');
    }
    if (filter.field === 'exchange' && value !== 'SSE' && value !== 'SZSE') {
      addIssue(context, 'ETF exchange filter is invalid');
    }
    if (
      filter.field === 'symbol' &&
      !(
        (filter.operator === 'EQ' && /^[0-9]{6}$/.test(value)) ||
        (filter.operator === 'PREFIX' && /^[0-9]{1,6}$/.test(value))
      )
    ) {
      addIssue(context, 'ETF symbol filter is invalid');
    }
    if (filter.field === 'displayName' && value.trim().length === 0) {
      addIssue(context, 'ETF displayName filter is invalid');
    }
    if (
      filter.field === 'listingStatus' &&
      !['LISTED', 'SUSPENDED', 'DELISTED', 'UNKNOWN'].includes(value)
    ) {
      addIssue(context, 'ETF listingStatus filter is invalid');
    }
    if (filter.field === 'navKind' && value !== 'UNIT' && value !== 'ACCUMULATED') {
      addIssue(context, 'ETF navKind filter is invalid');
    }
    if (
      filter.field === 'stateDimension' &&
      value !== 'TRADING' &&
      value !== 'SUBSCRIPTION' &&
      value !== 'REDEMPTION'
    ) {
      addIssue(context, 'ETF stateDimension filter is invalid');
    }
  }
}

/** 要求目录按单一交易所查询，详情数据按单一 ETF UUID 查询。 */
function validateRequiredEtfFilters(
  dataset: EtfMarketDataset,
  filters: readonly z.infer<typeof filterSchema>[],
  context: RefinementCtx,
): void {
  if (dataset === 'fund.etf.profile.reported') {
    const exchange = findFilter(filters, 'exchange');
    if (exchange === undefined || exchange.operator !== 'EQ' || exchange.values.length !== 1) {
      addIssue(context, 'ETF profile requires one exchange EQ filter');
    }
    return;
  }
  const entity = findFilter(filters, 'etfEntityRef');
  if (entity === undefined || entity.operator !== 'EQ' || entity.values.length !== 1) {
    addIssue(context, 'ETF detail query requires one etfEntityRef EQ filter');
  }
  if (dataset === 'fund.etf.nav.1d.reported' && findFilter(filters, 'navKind') === undefined) {
    addIssue(context, 'ETF NAV query requires an explicit navKind filter');
  }
}

/** 校验 ETF v2 显式 page 与页面用途上限，游标仍由 data-sync 绑定 publication。 */
function validateEtfPage(
  dataset: EtfMarketDataset,
  page: z.infer<typeof pageSchema> | undefined,
  context: RefinementCtx,
): void {
  if (page?.limit === undefined) {
    addIssue(context, 'ETF v2 page.limit is required');
    return;
  }
  const maximum =
    dataset === 'fund.etf.profile.reported'
      ? 50
      : dataset === 'fund.etf.bar.1d.reported' || dataset === 'fund.etf.nav.1d.reported'
        ? 366
        : 500;
  if (page.limit > maximum) addIssue(context, 'ETF v2 page.limit exceeds the dataset maximum');
}

/** 返回指定字段的唯一过滤器；重复字段已在通用校验阶段拒绝。 */
function findFilter(
  filters: readonly z.infer<typeof filterSchema>[],
  field: string,
): z.infer<typeof filterSchema> | undefined {
  for (const filter of filters) {
    if (filter.field === field) return filter;
  }
  return undefined;
}

/** 计算包含两端的自然日数量，输入已由 ISO date schema 校验。 */
function inclusiveCalendarDays(from: string, to: string): number {
  const fromMilliseconds = Date.parse(`${from}T00:00:00Z`);
  const toMilliseconds = Date.parse(`${to}T00:00:00Z`);
  return Math.floor((toMilliseconds - fromMilliseconds) / 86_400_000) + 1;
}

/** 判断字符串数组是否包含重复值。 */
function hasDuplicates(values: readonly string[]): boolean {
  return new Set(values).size !== values.length;
}

/** 向 Zod refinement 添加稳定的自定义合同问题。 */
function addIssue(context: RefinementCtx, message: string): void {
  context.addIssue({ code: 'custom', message });
}

/** 判断代码是否属于一期 ETF dataset，禁止用代码前缀猜测基金类型。 */
function isEtfMarketDataset(value: string): value is EtfMarketDataset {
  return (
    value === 'fund.etf.profile.reported' ||
    value === 'fund.etf.bar.1d.reported' ||
    value === 'fund.etf.nav.1d.reported' ||
    value === 'fund.etf.trading_state.reported'
  );
}

/** 定义公开服务支持的 data-sync 市场查询请求及 ETF v2 严格约束。 */
export const marketDataQueryRequestSchema = marketDataQueryRequestBaseSchema.superRefine(
  validateMarketDataQueryRequest,
);

/** 表示公开服务支持的 data-sync 市场查询请求。 */
export type MarketDataQueryRequest = z.infer<typeof marketDataQueryRequestSchema>;

/** 定义公开 publication 的来源投影，禁止 raw URI、Adapter 名或凭据越界。 */
const marketDataSourceSchema = z
  .object({
    sourceRef: z.string().min(1).max(120),
    publisher: z.string().min(1).max(200),
    sourceDataset: z.string().min(1).max(200),
    authoritative: z.boolean(),
    redistribution: z.string().min(1).max(80),
    coverageNote: z.string().min(1).max(500).nullable(),
  })
  .strict();

/** 定义有 canonical publication 时不可变发布元数据的安全投影。 */
const availableReleaseSchema = z
  .object({
    dataVersion: uuidSchema,
    publishedAt: dateTimeSchema,
    knowledgeCutoff: dateTimeSchema,
    publicUsableAt: dateTimeSchema,
    effectiveFrom: dateTimeSchema.nullable(),
    effectiveTo: dateTimeSchema.nullable(),
    methodology: z
      .object({
        code: z.string().min(1).max(100),
        version: z.string().min(1).max(40),
        kind: z.enum(['REPORTED', 'DERIVED', 'UNKNOWN']),
      })
      .strict(),
    sources: z.array(marketDataSourceSchema).max(32),
    quality: z
      .object({
        status: z.enum(['PASSED', 'WARNED']),
        issueCodes: z.array(z.string().min(1).max(120)).max(100),
      })
      .strict(),
    completeness: z.enum(['COMPLETE', 'PARTIAL', 'UNKNOWN']),
    disclaimers: z.array(z.string().min(1).max(1_000)).max(50),
  })
  .strict();

/** 定义无可读记录、来源不可用或当前明确不支持时的成功非数据结果元数据。 */
const emptyReleaseSchema = z
  .object({
    state: z.enum(['EMPTY', 'SOURCE_UNAVAILABLE', 'CURRENTLY_UNSUPPORTED']),
    observedAt: dateTimeSchema.nullable(),
    reasonCode: z.enum([
      'NO_MATCHING_FACTS',
      'PROVIDER_UNAVAILABLE',
      'CAPABILITY_NOT_CONFIGURED',
      'PUBLICATION_NOT_AVAILABLE',
      'NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET',
    ]),
  })
  .strict();

/** 定义 service-api 可以安全返回给前端的异构 typed-record 页面。 */
export const marketDataQueryResponseSchema = z
  .object({
    meta: z
      .object({
        requestId: requestIdSchema,
        contractVersion: z.literal('1.0.0'),
        dataset: z
          .object({
            code: z.string().regex(/^[a-z][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)+$/),
            schemaVersion: z.number().int().positive(),
          })
          .strict(),
        availability: z.enum(['AVAILABLE', 'EMPTY', 'SOURCE_UNAVAILABLE', 'CURRENTLY_UNSUPPORTED']),
        release: z.union([availableReleaseSchema, emptyReleaseSchema]),
        visibility: jsonObjectSchema,
        page: z
          .object({
            limit: z.number().int().positive().max(500),
            hasMore: z.boolean(),
            nextCursor: z.string().min(1).max(2_048).nullable(),
          })
          .strict(),
        coverage: jsonObjectSchema,
        warnings: z.array(z.string().min(1).max(500)).max(100),
        disclaimers: z.array(z.string().min(1).max(1_000)).max(50),
      })
      .strict(),
    records: z.array(jsonObjectSchema).max(500),
  })
  .strict();

/** 表示 service-api 公开市场数据查询的合同化响应。 */
export type MarketDataQueryResponse = z.infer<typeof marketDataQueryResponseSchema>;

/** 定义 ETF v2 record 的交易所限定标识，禁止只用六位代码猜测场所。 */
const etfIdentifierSchema = z
  .object({
    scheme: z.literal('venue_symbol'),
    value: z.string().regex(/^(SSE|SZSE)\.[0-9]{6}$/),
  })
  .strict();

/** 定义 ETF v2 record 的 canonical 实体身份。 */
const etfEntitySchema = z
  .object({
    entityRef: uuidSchema,
    entityType: z.literal('ETF_LISTING'),
    identifiers: z.array(etfIdentifierSchema).length(1),
  })
  .strict();

/** 定义 ETF v2 record 的不可变 revision 信息。 */
const revisionSchema = z
  .object({
    revisionNumber: z.number().int().positive(),
    currentInPublication: z.literal(true),
  })
  .strict();

/** 复用 ETF v2 typed-record 外壳，业务值始终留在 values 内。 */
const etfRecordEnvelopeFields = {
  entity: etfEntitySchema,
  publicUsableAt: dateTimeSchema,
  availabilityBasis: z.string().min(1).max(80),
  sourcePublishedAt: dateTimeSchema.nullable(),
  observedAt: dateTimeSchema,
  dataVersion: uuidSchema,
  sourceRef: z.string().min(1).max(120),
  methodologyVersion: z.literal('1'),
  qualityStatus: z.enum(['PASSED', 'WARNED']),
  revision: revisionSchema,
};

/** 定义 ETF 产品资料可按请求投影的严格 values。 */
const etfProfileValuesSchema = z
  .object({
    etfEntityRef: uuidSchema,
    exchange: z.enum(['SSE', 'SZSE']),
    symbol: z.string().regex(/^[0-9]{6}$/),
    displayName: z.string().trim().min(1).max(160),
    etfType: z.string().trim().min(1).max(80),
    managementMode: z.string().trim().min(1).max(80),
    managerName: z.string().trim().min(1).max(160).nullable(),
    custodianName: z.string().trim().min(1).max(160).nullable(),
    listedOn: dateSchema.nullable(),
    delistedOn: dateSchema.nullable(),
    listingStatus: z.enum(['LISTED', 'SUSPENDED', 'DELISTED', 'UNKNOWN']),
    quoteCurrency: currencySchema,
    navCurrency: currencySchema,
    sourceTimePrecision: z.enum(['EXACT', 'DATE_ONLY', 'UNKNOWN']),
  })
  .partial()
  .strict();

/** 定义 ETF 未复权日线可按请求投影的严格 values。 */
const etfBarValuesSchema = z
  .object({
    tradeDate: dateSchema,
    etfEntityRef: uuidSchema,
    open: nonNegativeDecimalSchema,
    high: nonNegativeDecimalSchema,
    low: nonNegativeDecimalSchema,
    close: nonNegativeDecimalSchema,
    volume: nonNegativeDecimalSchema,
    volumeUnit: z.string().trim().min(1).max(40),
    amount: nonNegativeDecimalSchema,
    currency: currencySchema,
    tradeStatus: z.string().trim().min(1).max(80).nullable(),
    adjustment: z.literal('UNADJUSTED'),
  })
  .partial()
  .strict();

/** 定义 ETF 来源直报 NAV 可按请求投影的严格 values。 */
const etfNavValuesSchema = z
  .object({
    navDate: dateSchema,
    etfEntityRef: uuidSchema,
    navKind: z.enum(['UNIT', 'ACCUMULATED']),
    nav: nonNegativeDecimalSchema,
    currency: currencySchema,
    finality: z.enum(['FINAL', 'PROVISIONAL', 'UNKNOWN']),
  })
  .partial()
  .strict();

/** 定义 ETF 交易、申购和赎回独立状态可按请求投影的严格 values。 */
const etfStateValuesSchema = z
  .object({
    etfEntityRef: uuidSchema,
    stateDimension: z.enum(['TRADING', 'SUBSCRIPTION', 'REDEMPTION']),
    state: z.string().trim().min(1).max(80),
    effectiveFrom: dateSchema,
    effectiveTo: dateSchema.nullable(),
    reason: z.string().trim().min(1).max(500).nullable(),
  })
  .partial()
  .strict();

/** 定义 ETF 产品资料标准 record 外壳。 */
const etfProfileRecordSchema = z
  .object({
    ...etfRecordEnvelopeFields,
    recordRef: z.string().min(1).max(300).startsWith('etf-profile:'),
    recordType: z.literal('ETF_PROFILE'),
    time: z.object({ effectiveFrom: dateSchema }).strict(),
    values: etfProfileValuesSchema,
  })
  .strict();

/** 定义 ETF 未复权日线标准 record 外壳。 */
const etfBarRecordSchema = z
  .object({
    ...etfRecordEnvelopeFields,
    recordRef: z.string().min(1).max(300).startsWith('etf-bar:'),
    recordType: z.literal('ETF'),
    time: z.object({ tradeDate: dateSchema }).strict(),
    values: etfBarValuesSchema,
  })
  .strict();

/** 定义 ETF NAV 标准 record 外壳。 */
const etfNavRecordSchema = z
  .object({
    ...etfRecordEnvelopeFields,
    recordRef: z.string().min(1).max(300).startsWith('etf-nav:'),
    recordType: z.literal('ETF'),
    time: z.object({ navDate: dateSchema }).strict(),
    values: etfNavValuesSchema,
  })
  .strict();

/** 定义 ETF 状态标准 record 外壳。 */
const etfStateRecordSchema = z
  .object({
    ...etfRecordEnvelopeFields,
    recordRef: z.string().min(1).max(300).startsWith('etf-status:'),
    recordType: z.literal('ETF_STATUS'),
    time: z.object({ effectiveFrom: dateSchema }).strict(),
    values: etfStateValuesSchema,
  })
  .strict();

/** 表示通过 ETF v2 标准外壳校验后的公共结构。 */
type ParsedEtfRecord = {
  entity: { entityRef: string; identifiers: Array<{ value: string }> };
  time: Record<string, string>;
  dataVersion: string;
  sourceRef: string;
  values: Record<string, unknown>;
} & Record<string, unknown>;

/** 在通用 envelope 后校验请求绑定、availability 和 ETF v2 typed records。 */
export function parseMarketDataQueryResponse(
  input: unknown,
  request: MarketDataQueryRequest,
): MarketDataQueryResponse {
  const response = marketDataQueryResponseSchema.parse(input);
  validateResponseEnvelope(response, request);
  if (
    response.meta.availability !== 'AVAILABLE' ||
    request.dataset.schemaVersion !== 2 ||
    !isEtfMarketDataset(request.dataset.code)
  ) {
    return response;
  }
  const release = response.meta.release;
  if (!('dataVersion' in release)) throw new Error('available market data has no release');
  const records = parseEtfV2Records(
    request.dataset.code,
    request.fields,
    response.records,
    release.dataVersion,
    release.sources,
  );
  return { ...response, records };
}

/** 校验响应 dataset、schemaVersion、availability、release 和记录版本保持请求一致。 */
function validateResponseEnvelope(
  response: MarketDataQueryResponse,
  request: MarketDataQueryRequest,
): void {
  if (
    response.meta.dataset.code !== request.dataset.code ||
    response.meta.dataset.schemaVersion !== request.dataset.schemaVersion
  ) {
    throw new Error('market data response dataset does not match request');
  }
  if (request.dataset.schemaVersion === 2 && isEtfMarketDataset(request.dataset.code)) {
    validateEtfV2ResponseBinding(response, request);
  }
  if (response.meta.availability === 'AVAILABLE') {
    if (!('dataVersion' in response.meta.release)) {
      throw new Error('available market data response has no dataVersion');
    }
    for (const record of response.records) {
      if (record.dataVersion !== response.meta.release.dataVersion) {
        throw new Error('market data record does not match release dataVersion');
      }
    }
    return;
  }
  if (
    'dataVersion' in response.meta.release ||
    response.meta.release.state !== response.meta.availability ||
    response.records.length !== 0 ||
    response.meta.page.hasMore ||
    response.meta.page.nextCursor !== null
  ) {
    throw new Error('unavailable market data response is inconsistent');
  }
}

/** 将 ETF v2 响应绑定到当前请求的可见性、分页、质量、精确版本和单一冻结来源。 */
function validateEtfV2ResponseBinding(
  response: MarketDataQueryResponse,
  request: MarketDataQueryRequest,
): void {
  const requestedVisibility = currentVisibilitySchema.parse(request.visibility);
  if (
    response.meta.visibility.mode !== requestedVisibility.mode ||
    Object.keys(response.meta.visibility).length !== 1
  ) {
    throw new Error('ETF response visibility does not match request');
  }
  const requestedLimit = request.page?.limit;
  if (
    requestedLimit === undefined ||
    response.meta.page.limit !== requestedLimit ||
    response.records.length > requestedLimit
  ) {
    throw new Error('ETF response page does not match request');
  }
  if (response.meta.page.hasMore !== (response.meta.page.nextCursor !== null)) {
    throw new Error('ETF response cursor state is inconsistent');
  }
  if (response.meta.availability !== 'AVAILABLE') {
    const release = response.meta.release;
    if ('dataVersion' in release) {
      throw new Error('unavailable ETF response has an available release');
    }
    const validReason =
      (response.meta.availability === 'EMPTY' && release.reasonCode === 'NO_MATCHING_FACTS') ||
      (response.meta.availability === 'SOURCE_UNAVAILABLE' &&
        ['PROVIDER_UNAVAILABLE', 'CAPABILITY_NOT_CONFIGURED', 'PUBLICATION_NOT_AVAILABLE'].includes(
          release.reasonCode,
        )) ||
      (response.meta.availability === 'CURRENTLY_UNSUPPORTED' &&
        request.dataset.code === 'fund.etf.nav.1d.reported' &&
        release.reasonCode === 'NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET');
    if (!validReason) {
      throw new Error('ETF unavailable reason does not match dataset or availability');
    }
    return;
  }
  const release = response.meta.release;
  if (!('dataVersion' in release)) {
    throw new Error('available ETF response has no release');
  }
  const selection = etfSelectionSchema.parse(request.selection);
  if (!selection.qualityStatuses.includes(release.quality.status)) {
    throw new Error('ETF release quality does not satisfy request');
  }
  if (selection.dataVersion !== undefined && release.dataVersion !== selection.dataVersion) {
    throw new Error('ETF release dataVersion does not match request');
  }
  if (release.sources.length !== 1) {
    throw new Error('ETF v2 response requires exactly one frozen public source');
  }
}

/** 按 dataset 解析 ETF v2 records，并验证字段投影、来源及 canonical 身份。 */
function parseEtfV2Records(
  dataset: EtfMarketDataset,
  selectedFields: readonly string[],
  inputs: readonly Record<string, unknown>[],
  dataVersion: string,
  sources: readonly z.infer<typeof marketDataSourceSchema>[],
): Record<string, unknown>[] {
  const sourceRefs = new Set<string>();
  for (const source of sources) sourceRefs.add(source.sourceRef);
  const records: Record<string, unknown>[] = [];
  for (const input of inputs) {
    const record = parseEtfV2Record(dataset, input);
    validateEtfV2Record(dataset, record, selectedFields, dataVersion, sourceRefs);
    records.push(record);
  }
  return records;
}

/** 使用 dataset-specific schema 解析一条 ETF v2 标准 record。 */
function parseEtfV2Record(dataset: EtfMarketDataset, input: unknown): ParsedEtfRecord {
  if (dataset === 'fund.etf.profile.reported') {
    return etfProfileRecordSchema.parse(input);
  }
  if (dataset === 'fund.etf.bar.1d.reported') {
    return etfBarRecordSchema.parse(input);
  }
  if (dataset === 'fund.etf.nav.1d.reported') {
    return etfNavRecordSchema.parse(input);
  }
  return etfStateRecordSchema.parse(input);
}

/** 校验一条 ETF v2 record 的字段集合、dataVersion、sourceRef、实体和业务时间。 */
function validateEtfV2Record(
  dataset: EtfMarketDataset,
  record: ParsedEtfRecord,
  selectedFields: readonly string[],
  dataVersion: string,
  sourceRefs: ReadonlySet<string>,
): void {
  const actualFields = Object.keys(record.values);
  if (!sameStringSet(actualFields, selectedFields)) {
    throw new Error('ETF record values do not match selected fields');
  }
  if (record.dataVersion !== dataVersion || !sourceRefs.has(record.sourceRef)) {
    throw new Error('ETF record release lineage is inconsistent');
  }
  const valueEntityRef = record.values.etfEntityRef;
  if (valueEntityRef !== undefined && valueEntityRef !== record.entity.entityRef) {
    throw new Error('ETF record entityRef is inconsistent');
  }
  validateEtfRecordTime(dataset, record);
  if (dataset === 'fund.etf.profile.reported') validateEtfProfileIdentity(record);
}

/** 校验 record 外层业务时间与 values 中被选择的同名时间完全一致。 */
function validateEtfRecordTime(dataset: EtfMarketDataset, record: ParsedEtfRecord): void {
  const field =
    dataset === 'fund.etf.profile.reported' || dataset === 'fund.etf.trading_state.reported'
      ? 'effectiveFrom'
      : dataset === 'fund.etf.bar.1d.reported'
        ? 'tradeDate'
        : 'navDate';
  const selectedTime = record.values[field];
  if (selectedTime !== undefined && selectedTime !== record.time[field]) {
    throw new Error('ETF record time is inconsistent');
  }
}

/** 校验 profile 的 exchange/symbol 与交易所限定 identifier 一致。 */
function validateEtfProfileIdentity(record: ParsedEtfRecord): void {
  const exchange = record.values.exchange;
  const symbol = record.values.symbol;
  if (
    typeof exchange === 'string' &&
    typeof symbol === 'string' &&
    record.entity.identifiers[0]?.value !== `${exchange}.${symbol}`
  ) {
    throw new Error('ETF profile identifier is inconsistent');
  }
}

/** 判断两个字符串数组是否表示完全相同的唯一集合。 */
function sameStringSet(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) return false;
  const expected = new Set(right);
  for (const value of left) {
    if (!expected.has(value)) return false;
  }
  return true;
}
