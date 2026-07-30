import { z } from 'zod';

/** 约束公开与内部合同使用的 ISO 业务日期。 */
const dateSchema = z.string().date();

/** 约束公开与内部合同使用的带偏移 RFC 3339 时间。 */
const dateTimeSchema = z.string().datetime({ offset: true });

/** 表示可安全保存在 JSONB 中的未知对象，但不接受数组或标量顶层值。 */
export const jsonObjectSchema = z.record(z.string(), z.unknown());

/** 表示同步 target 支持的四种权威模式。 */
export const syncModeSchema = z.enum(['FULL', 'INCREMENTAL', 'DATE_RANGE', 'OBSERVATION_DATE']);

/** 约束沪深北股票标的使用的交易所枚举。 */
const equityExchangeSchema = z.enum(['SSE', 'SZSE', 'BSE']);

/** 约束期货合约标的使用的交易场所枚举。 */
const futuresVenueSchema = z.enum(['CFFEX', 'SHFE', 'DCE', 'CZCE', 'INE']);

/** 冻结四个 ETF canonical dataset 与唯一合法同步操作的映射。 */
const etfOperationByDatasetCode = {
  'fund.etf.profile.reported': 'MASTER',
  'fund.etf.trading_state.reported': 'STATUS',
  'fund.etf.bar.1d.reported': 'BARS',
  'fund.etf.nav.1d.reported': 'NAV',
} as const;

/** 表示全量数据集唯一允许的全局选择器。 */
const globalTargetSelectorSchema = z.object({ kind: z.literal('GLOBAL') }).strict();

/** 表示单个沪深北证券的严格业务选择器。 */
const instrumentTargetSelectorSchema = z
  .object({
    kind: z.literal('INSTRUMENT'),
    exchange: equityExchangeSchema,
    symbol: z.string().regex(/^[0-9A-Z.-]{1,32}$/),
  })
  .strict();

/** 表示行业体系下单个行业的严格业务选择器。 */
const sectorTargetSelectorSchema = z
  .object({
    kind: z.literal('SECTOR'),
    scheme: z.string().min(1).max(64),
    sectorCode: z.string().min(1).max(120),
  })
  .strict();

/** 表示行业体系本身的严格业务选择器。 */
const schemeTargetSelectorSchema = z
  .object({ kind: z.literal('SCHEME'), scheme: z.string().min(1).max(64) })
  .strict();

/** 表示交易所维度的严格业务选择器。 */
const exchangeTargetSelectorSchema = z
  .object({ kind: z.literal('EXCHANGE'), exchange: equityExchangeSchema })
  .strict();

/** 表示单个期货合约的严格业务选择器。 */
const contractTargetSelectorSchema = z
  .object({
    kind: z.literal('CONTRACT'),
    venue: futuresVenueSchema,
    contract: z
      .string()
      .min(1)
      .max(64)
      .regex(/^[0-9A-Z._-]+$/),
  })
  .strict();

/** 冻结沪深两市 ETF profile publication，防止全量 fan-out 在预检后漂移。 */
export const etfProfileDataVersionsSchema = z
  .object({
    SSE: z.string().uuid(),
    SZSE: z.string().uuid(),
  })
  .strict();

/** 表示 ETF 主数据、单只或全部已发布 ETF 能力的严格业务选择器。 */
const etfTargetSelectorSchema = z
  .object({
    kind: z.literal('ETF'),
    operation: z.enum(['MASTER', 'STATUS', 'BARS', 'NAV']),
    venue: z.enum(['SSE', 'SZSE']).nullable(),
    scope: z.enum(['ALL_VENUES', 'ALL_ETFS']).optional(),
    etf: z
      .string()
      .regex(/^(SSE|SZSE)\.[0-9]{6}$/)
      .nullable(),
    profileDataVersions: z.union([z.null(), etfProfileDataVersionsSchema]).optional(),
  })
  .strict()
  .superRefine((value, context) => {
    // MASTER 保留单市场兼容形状，同时允许唯一 schedule 显式覆盖沪深两市。
    if (value.operation === 'MASTER') {
      const isLegacySingleVenue = value.venue !== null && value.scope === undefined;
      const isAllVenues = value.venue === null && value.scope === 'ALL_VENUES';
      if (
        value.etf !== null ||
        value.profileDataVersions !== undefined ||
        (!isLegacySingleVenue && !isAllVenues)
      ) {
        context.addIssue({
          code: 'custom',
          message: 'ETF MASTER requires one venue or the exact ALL_VENUES scope',
        });
      }
      return;
    }
    if (value.etf !== null) {
      if (value.scope !== undefined || value.profileDataVersions !== undefined) {
        context.addIssue({
          code: 'custom',
          message: 'single ETF operation keeps the legacy venue and etf shape',
        });
      }
      if (value.venue !== null && !value.etf.startsWith(`${value.venue}.`)) {
        context.addIssue({
          code: 'custom',
          path: ['venue'],
          message: 'single ETF venue must match the qualified ETF identity',
        });
      }
      return;
    }
    if (
      value.venue !== null ||
      value.scope !== 'ALL_ETFS' ||
      value.profileDataVersions === undefined
    ) {
      context.addIssue({
        code: 'custom',
        message: 'ETF ALL_ETFS requires null venue, null etf and profileDataVersions',
      });
    }
  });

/** 表示融资融券市场、证券或资格能力的严格业务选择器。 */
const marginTargetSelectorSchema = z
  .object({
    kind: z.literal('MARGIN'),
    operation: z.enum(['MARKET', 'SECURITY', 'ELIGIBILITY']),
    venue: z.enum(['SSE', 'SZSE']),
    security: z.union([z.null(), instrumentTargetSelectorSchema]),
  })
  .strict();

/** 表示一次同步市场统计、活跃榜、状态和身份的沪深港通完整 bundle 选择器。 */
const stockConnectTargetSelectorSchema = z
  .object({
    kind: z.literal('STOCK_CONNECT'),
    operation: z.literal('MARKET'),
    channel: z.enum(['SH', 'SZ', 'ALL']),
    direction: z.enum(['NORTHBOUND', 'SOUTHBOUND']).nullable(),
  })
  .strict();

/** 表示龙虎榜或大宗交易事件的严格业务选择器。 */
const tradingEventTargetSelectorSchema = z
  .object({ kind: z.literal('TRADING_EVENT'), operation: z.enum(['DRAGON_TIGER', 'BLOCK_TRADE']) })
  .strict();

/** 表示指数编制方、能力及指数代码的严格业务选择器。 */
const indexTargetSelectorSchema = z
  .object({
    kind: z.literal('INDEX'),
    administrator: z.enum(['CSI', 'CNI']),
    capability: z.string().min(1).max(120),
    indexCode: z.string().min(1).max(64),
  })
  .strict();

/** 表示合同允许的受限业务目标选择器并集，禁止透传 Provider 参数或 URI。 */
export const targetSelectorSchema = z.discriminatedUnion('kind', [
  globalTargetSelectorSchema,
  instrumentTargetSelectorSchema,
  sectorTargetSelectorSchema,
  schemeTargetSelectorSchema,
  exchangeTargetSelectorSchema,
  contractTargetSelectorSchema,
  etfTargetSelectorSchema,
  marginTargetSelectorSchema,
  stockConnectTargetSelectorSchema,
  tradingEventTargetSelectorSchema,
  indexTargetSelectorSchema,
]);

/** 校验 preflight 草稿与 schedule 模板中的全量 ETF 尚未冻结 publication。 */
export const draftTargetSelectorSchema = targetSelectorSchema.superRefine((selector, context) => {
  if (
    selector.kind === 'ETF' &&
    selector.operation !== 'MASTER' &&
    selector.scope === 'ALL_ETFS' &&
    selector.profileDataVersions !== null
  ) {
    context.addIssue({
      code: 'custom',
      path: ['profileDataVersions'],
      message: 'ETF draft selector requires null profileDataVersions',
    });
  }
});

/** 判断 selector 是否与 canonical dataset 的固定 ETF 操作一致，其他数据集禁止使用 ETF selector。 */
export function targetSelectorMatchesDataset(
  datasetCode: string,
  selector: z.infer<typeof targetSelectorSchema>,
): boolean {
  const expectedEtfOperation =
    etfOperationByDatasetCode[datasetCode as keyof typeof etfOperationByDatasetCode];
  return expectedEtfOperation === undefined
    ? selector.kind !== 'ETF'
    : selector.kind === 'ETF' && selector.operation === expectedEtfOperation;
}

/** 校验一个同步 target 的模式与日期参数组合。 */
export const syncTargetSchema = z
  .object({
    datasetCode: z.string().trim().min(1).max(160),
    mode: syncModeSchema,
    selector: targetSelectorSchema,
    dateFrom: dateSchema.nullable().optional(),
    dateTo: dateSchema.nullable().optional(),
    observationDate: dateSchema.nullable().optional(),
  })
  .strict()
  .superRefine((value, context) => {
    if (!targetSelectorMatchesDataset(value.datasetCode, value.selector)) {
      context.addIssue({
        code: 'custom',
        path: ['selector', 'operation'],
        message: 'ETF datasetCode and selector operation do not match',
      });
    }
    // 四种模式的日期字段必须完整显式，避免 API 替 data-sync 猜测范围。
    const dateFrom = value.dateFrom ?? null;
    const dateTo = value.dateTo ?? null;
    const observationDate = value.observationDate ?? null;
    if (value.mode === 'DATE_RANGE') {
      if (dateFrom === null || dateTo === null || observationDate !== null) {
        context.addIssue({
          code: 'custom',
          message: 'DATE_RANGE requires dateFrom and dateTo only',
        });
      }
      return;
    }
    if (value.mode === 'OBSERVATION_DATE') {
      if (dateFrom !== null || dateTo !== null || observationDate === null) {
        context.addIssue({
          code: 'custom',
          message: 'OBSERVATION_DATE requires observationDate only',
        });
      }
      return;
    }
    if (dateFrom !== null || dateTo !== null || observationDate !== null) {
      context.addIssue({ code: 'custom', message: 'FULL and INCREMENTAL do not accept dates' });
    }
  });

/** 校验预检请求中的 ETF selector 尚未绑定 profile publication 版本。 */
export const syncPreflightTargetSchema = syncTargetSchema.superRefine((value, context) => {
  const selector = value.selector;
  if (
    selector.kind === 'ETF' &&
    selector.operation !== 'MASTER' &&
    selector.scope === 'ALL_ETFS' &&
    selector.profileDataVersions !== null
  ) {
    context.addIssue({
      code: 'custom',
      path: ['selector', 'profileDataVersions'],
      message: 'ETF preflight requires null profileDataVersions',
    });
  }
});

/** 校验预检返回及提交中的 ETF 全量 selector 已冻结沪深 profile publication。 */
export const syncFrozenTargetSchema = syncTargetSchema.superRefine((value, context) => {
  const selector = value.selector;
  if (
    selector.kind === 'ETF' &&
    selector.operation !== 'MASTER' &&
    selector.scope === 'ALL_ETFS' &&
    selector.profileDataVersions === null
  ) {
    context.addIssue({
      code: 'custom',
      path: ['selector', 'profileDataVersions'],
      message: 'ETF ALL_ETFS submission requires frozen profileDataVersions',
    });
  }
});

/** 校验带游标的公开检索请求公共字段。 */
export const cursorPageRequestSchema = z
  .object({
    cursor: z.string().max(1024).nullable().optional(),
    limit: z.number().int().min(1).max(100).optional(),
  })
  .strict();

/** 校验数据资产筛选条件及其合同边界。 */
export const datasetSearchRequestSchema = cursorPageRequestSchema
  .extend({
    query: z.string().max(120).nullable().optional(),
    domains: z.array(z.string().min(1).max(80)).max(20).optional(),
    providers: z.array(z.string().min(1).max(80)).max(20).optional(),
    upstreamSources: z.array(z.string().min(1).max(120)).max(20).optional(),
    availability: z
      .array(z.enum(['ENABLED', 'DISABLED', 'SOURCE_UNAVAILABLE', 'MODEL_ONLY', 'UNKNOWN']))
      .max(5)
      .optional(),
    observationStates: z
      .array(z.enum(['PRESENT', 'EMPTY_VALID', 'EMPTY_UNEXPECTED', 'NOT_YET_SYNCED', 'UNKNOWN']))
      .max(5)
      .optional(),
    runStatuses: z
      .array(
        z.enum([
          'QUEUED',
          'RUNNING',
          'CANCEL_REQUESTED',
          'SUCCEEDED',
          'PARTIAL',
          'FAILED',
          'CANCELLED',
          'INTERRUPTED',
          'SKIPPED',
        ]),
      )
      .max(9)
      .optional(),
    healthStatuses: z
      .array(z.enum(['HEALTHY', 'WARN', 'CRITICAL', 'UNKNOWN']))
      .max(4)
      .optional(),
  })
  .strict();

/** 校验数据集详情的稳定身份。 */
export const datasetDetailRequestSchema = z
  .object({ datasetCode: z.string().trim().min(1).max(160) })
  .strict();

/** 校验 1–100 个同步 target，并拒绝同批重复数据集。 */
export const syncPreflightRequestSchema = z
  .object({ targets: z.array(syncPreflightTargetSchema).min(1).max(100) })
  .strict()
  .superRefine((value, context) => {
    // 同批重复 target 会使顺序、取消和重试语义不再确定，必须在 API 拒绝。
    const duplicates = duplicateDatasetCodes(value.targets);
    if (duplicates.length > 0) {
      context.addIssue({
        code: 'custom',
        message: `Duplicate datasetCode: ${duplicates.join(',')}`,
      });
    }
  });

/** 校验写操作必须记录的人工理由。 */
const reasonSchema = z.string().trim().min(2).max(500);

/** 校验预检后提交的同步意图，并保留原 target 顺序。 */
export const syncSubmitRequestSchema = z
  .object({
    preflightId: z.string().uuid(),
    requestHash: z.string().regex(/^[0-9a-f]{64}$/),
    targets: z.array(syncFrozenTargetSchema).min(1).max(100),
    reason: reasonSchema,
  })
  .strict()
  .superRefine((value, context) => {
    // 预检冻结顺序由 data-sync 决定，API 只确保重复数据集不会进入 outbox。
    const duplicates = duplicateDatasetCodes(value.targets);
    if (duplicates.length > 0) {
      context.addIssue({
        code: 'custom',
        message: `Duplicate datasetCode: ${duplicates.join(',')}`,
      });
    }
  });

/** 校验 COMMAND 与 RUN 的显式动作作用域。 */
export const commandActionTargetSchema = z
  .object({
    resourceType: z.enum(['COMMAND', 'RUN']),
    resourceId: z.string().uuid(),
  })
  .strict();

/** 校验取消或重试请求的目标和理由。 */
export const commandActionRequestSchema = z
  .object({ target: commandActionTargetSchema, reason: reasonSchema })
  .strict();

/** 校验命令详情的稳定引用。 */
export const commandDetailRequestSchema = z.object({ commandId: z.string().uuid() }).strict();

/** 校验 run 搜索的筛选与公开分页上限。 */
export const runSearchRequestSchema = cursorPageRequestSchema
  .extend({
    datasetCodes: z.array(z.string().min(1).max(160)).max(100).optional(),
    statuses: z
      .array(
        z.enum([
          'QUEUED',
          'RUNNING',
          'CANCEL_REQUESTED',
          'SUCCEEDED',
          'PARTIAL',
          'FAILED',
          'CANCELLED',
          'INTERRUPTED',
          'SKIPPED',
        ]),
      )
      .max(9)
      .optional(),
    requestedFrom: dateTimeSchema.nullable().optional(),
    requestedTo: dateTimeSchema.nullable().optional(),
  })
  .strict();

/** 校验 run 详情两组独立 cursor 的续页参数。 */
export const runDetailRequestSchema = z
  .object({
    runId: z.string().uuid(),
    partitionsCursor: z.string().max(1024).nullable().optional(),
    partitionsLimit: z.number().int().min(1).max(200).optional(),
    timelineCursor: z.string().max(1024).nullable().optional(),
    timelineLimit: z.number().int().min(1).max(200).optional(),
  })
  .strict();

/** 校验健康评估搜索条件。 */
export const healthSearchRequestSchema = cursorPageRequestSchema
  .extend({
    datasetCodes: z.array(z.string().min(1).max(160)).max(100).optional(),
    statuses: z
      .array(z.enum(['HEALTHY', 'WARN', 'CRITICAL', 'UNKNOWN']))
      .max(4)
      .optional(),
    evaluatedFrom: dateTimeSchema.nullable().optional(),
    evaluatedTo: dateTimeSchema.nullable().optional(),
  })
  .strict();

/** 校验健康详情及其独立问题游标。 */
export const healthDetailRequestSchema = z
  .object({
    evaluationId: z.string().uuid(),
    issuesCursor: z.string().max(1024).nullable().optional(),
    issuesLimit: z.number().int().min(1).max(100).optional(),
  })
  .strict();

/** 校验单个主动健康检查 target。 */
export const healthCheckTargetSchema = z
  .object({
    datasetCode: z.string().trim().min(1).max(160),
    dataVersion: z.string().uuid().nullable(),
  })
  .strict();

/** 校验主动健康检查批次并拒绝同一数据集重复检查。 */
export const healthCheckSubmitRequestSchema = z
  .object({ targets: z.array(healthCheckTargetSchema).min(1).max(100), reason: reasonSchema })
  .strict()
  .superRefine((value, context) => {
    // 同一批次重复检查同一个数据集会产生难以解释的有序结果。
    const duplicates = duplicateDatasetCodes(value.targets);
    if (duplicates.length > 0) {
      context.addIssue({
        code: 'custom',
        message: `Duplicate datasetCode: ${duplicates.join(',')}`,
      });
    }
  });

/** 校验健康检查批次详情引用。 */
export const healthCheckDetailRequestSchema = z
  .object({ healthCheckId: z.string().uuid() })
  .strict();

/** 校验自动计划搜索条件。 */
export const scheduleSearchRequestSchema = cursorPageRequestSchema
  .extend({
    datasetCodes: z.array(z.string().min(1).max(160)).max(100).optional(),
    enabled: z.boolean().nullable().optional(),
  })
  .strict();

/** 校验结构化自动计划频率，不接受任意 cron。 */
export const scheduleFrequencySchema = z
  .object({
    kind: z.enum(['TRADING_DAY', 'DAILY', 'WEEKLY', 'MONTHLY', 'INTERVAL']),
    timezone: z.string().trim().min(1).max(80),
    localTime: z
      .string()
      .regex(/^([01][0-9]|2[0-3]):[0-5][0-9]$/)
      .nullable(),
    dayOfWeek: z.number().int().min(1).max(7).nullable(),
    dayOfMonth: z.number().int().min(1).max(31).nullable(),
    intervalMinutes: z.number().int().min(5).max(43_200).nullable(),
    calendarCode: z.string().min(1).max(80).nullable(),
  })
  .strict()
  .superRefine((value, context) => {
    // 频率字段必须与 kind 一一对应，防止不受控的调度表达式落入数据库。
    const localTime = value.localTime ?? null;
    const dayOfWeek = value.dayOfWeek ?? null;
    const dayOfMonth = value.dayOfMonth ?? null;
    const intervalMinutes = value.intervalMinutes ?? null;
    const calendarCode = value.calendarCode ?? null;
    const invalid =
      (value.kind === 'TRADING_DAY' &&
        (localTime === null ||
          calendarCode === null ||
          dayOfWeek !== null ||
          dayOfMonth !== null ||
          intervalMinutes !== null)) ||
      (value.kind === 'DAILY' &&
        (localTime === null ||
          dayOfWeek !== null ||
          dayOfMonth !== null ||
          intervalMinutes !== null)) ||
      (value.kind === 'WEEKLY' &&
        (localTime === null ||
          dayOfWeek === null ||
          dayOfMonth !== null ||
          intervalMinutes !== null)) ||
      (value.kind === 'MONTHLY' &&
        (localTime === null ||
          dayOfWeek !== null ||
          dayOfMonth === null ||
          intervalMinutes !== null)) ||
      (value.kind === 'INTERVAL' &&
        (localTime !== null ||
          dayOfWeek !== null ||
          dayOfMonth !== null ||
          intervalMinutes === null ||
          calendarCode !== null));
    if (invalid) {
      context.addIssue({ code: 'custom', message: 'Schedule frequency fields do not match kind' });
    }
  });

/** 校验计划触发时冻结的目标日期解析策略。 */
export const scheduleTargetPolicySchema = z
  .object({
    policyVersion: z.number().int().min(1),
    dateResolution: z.enum(['NONE', 'SCHEDULED_LOCAL_DATE', 'LATEST_COMPLETED_TRADING_DATE']),
  })
  .strict();

/** 校验创建或更新计划的成对乐观锁字段。 */
export const scheduleUpsertRequestSchema = z
  .object({
    scheduleId: z.string().uuid().nullable(),
    datasetCode: z.string().trim().min(1).max(160),
    mode: z.enum(['FULL', 'INCREMENTAL', 'OBSERVATION_DATE']),
    selector: draftTargetSelectorSchema,
    targetPolicy: scheduleTargetPolicySchema,
    frequency: scheduleFrequencySchema,
    misfirePolicy: z.enum(['SKIP', 'RUN_ONCE']),
    coalesce: z.boolean(),
    enabled: z.boolean(),
    expectedVersion: z.number().int().min(1).nullable(),
    reason: reasonSchema,
  })
  .strict()
  .superRefine((value, context) => {
    if (!targetSelectorMatchesDataset(value.datasetCode, value.selector)) {
      context.addIssue({
        code: 'custom',
        path: ['selector', 'operation'],
        message: 'ETF datasetCode and selector operation do not match',
      });
    }
    // 创建与更新不能用半空版本字段绕过 data-sync 的乐观锁。
    if ((value.scheduleId === null) !== (value.expectedVersion === null)) {
      context.addIssue({
        code: 'custom',
        message: 'scheduleId and expectedVersion must both be null or non-null',
      });
    }
    const isSimpleMode = value.mode === 'FULL' || value.mode === 'INCREMENTAL';
    if (
      (isSimpleMode && value.targetPolicy.dateResolution !== 'NONE') ||
      (!isSimpleMode && value.targetPolicy.dateResolution === 'NONE')
    ) {
      context.addIssue({ code: 'custom', message: 'Schedule target policy does not match mode' });
    }
  });

/** 校验计划启停的版本与理由。 */
export const scheduleEnabledRequestSchema = z
  .object({
    scheduleId: z.string().uuid(),
    enabled: z.boolean(),
    expectedVersion: z.number().int().min(1),
    reason: reasonSchema,
  })
  .strict();

/** 校验本地 Submission 详情稳定引用。 */
export const submissionDetailRequestSchema = z.object({ submissionId: z.string().uuid() }).strict();

/** 校验数据运维记录搜索条件。 */
export const operationSearchRequestSchema = cursorPageRequestSchema
  .extend({
    actorIds: z.array(z.string().uuid()).max(100).optional(),
    actions: z.array(z.string().min(1).max(80)).max(20).optional(),
    deliveryStatuses: z
      .array(
        z.enum(['PENDING', 'DELIVERING', 'ACCEPTED', 'REJECTED', 'DEAD_LETTER', 'NOT_APPLICABLE']),
      )
      .max(6)
      .optional(),
    operationResults: z
      .array(
        z.enum([
          'UNKNOWN',
          'QUEUED',
          'RUNNING',
          'CANCEL_REQUESTED',
          'SUCCEEDED',
          'PARTIAL',
          'FAILED',
          'CANCELLED',
          'INTERRUPTED',
          'SKIPPED',
          'REJECTED',
        ]),
      )
      .max(11)
      .optional(),
    occurredFrom: dateTimeSchema.nullable().optional(),
    occurredTo: dateTimeSchema.nullable().optional(),
  })
  .strict();

/** 校验不含字段的总览请求，防止静默接受未知筛选。 */
export const emptyObjectRequestSchema = z.object({}).strict();

/** 校验内部 data-sync 返回的顶层 JSON 对象，细节再由公开投影逐字段裁剪。 */
export const internalDataOperationsResponseSchema = z.record(z.string(), z.unknown());

/** 描述公开合同允许的所有写请求。 */
export type DataOperationsWriteRequest =
  | z.infer<typeof syncSubmitRequestSchema>
  | z.infer<typeof commandActionRequestSchema>
  | z.infer<typeof healthCheckSubmitRequestSchema>
  | z.infer<typeof scheduleUpsertRequestSchema>
  | z.infer<typeof scheduleEnabledRequestSchema>;

/** 描述一个通过运行时校验的内部 JSON 对象。 */
export type InternalDataOperationsResponse = z.infer<typeof internalDataOperationsResponseSchema>;

/** 找出 target 数组中的重复数据集代码，保持第一次重复出现顺序。 */
function duplicateDatasetCodes(targets: ReadonlyArray<{ datasetCode: string }>): string[] {
  const seen = new Set<string>();
  const duplicates = new Set<string>();
  for (const target of targets) {
    if (seen.has(target.datasetCode)) {
      duplicates.add(target.datasetCode);
    }
    seen.add(target.datasetCode);
  }
  return [...duplicates];
}
