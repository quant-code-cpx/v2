/** 数据运维公开 API 使用的可追踪资源类型。 */
export type DataOperationResourceType = "COMMAND" | "RUN" | "HEALTH_CHECK" | "SCHEDULE";

/** 数据集在当前环境中的可用性。 */
export type DatasetAvailability =
  | "ENABLED"
  | "DISABLED"
  | "SOURCE_UNAVAILABLE"
  | "MODEL_ONLY"
  | "UNKNOWN";

/** 数据集的观测数据状态，不能与运行或健康结论混用。 */
export type DatasetObservationState =
  | "PRESENT"
  | "EMPTY_VALID"
  | "EMPTY_UNEXPECTED"
  | "NOT_YET_SYNCED"
  | "UNKNOWN";

/** 同步任务支持的固定执行模式。 */
export type SyncMode = "FULL" | "INCREMENTAL" | "DATE_RANGE" | "OBSERVATION_DATE";

/** 数据集 capability 允许的受限业务目标选择器类别。 */
export type TargetSelectorKind =
  | "GLOBAL"
  | "INSTRUMENT"
  | "SECTOR"
  | "SCHEME"
  | "EXCHANGE"
  | "CONTRACT"
  | "ETF"
  | "MARGIN"
  | "STOCK_CONNECT"
  | "STOCK_CONNECT_RESEARCH"
  | "TRADING_EVENT"
  | "INDEX"
  | "MONEY_FLOW";

/** 数据集服务端计算的新鲜度结论。 */
export type FreshnessStatus = "FRESH" | "WARNING" | "STALE" | "UNKNOWN" | "NOT_APPLICABLE";

/** 同步运行的权威状态。 */
export type RunStatus =
  | "QUEUED"
  | "RUNNING"
  | "CANCEL_REQUESTED"
  | "SUCCEEDED"
  | "PARTIAL"
  | "FAILED"
  | "CANCELLED"
  | "INTERRUPTED"
  | "SKIPPED";

/** 批量同步命令的权威聚合状态。 */
export type CommandStatus = Exclude<RunStatus, "INTERRUPTED" | "SKIPPED"> | "REJECTED";

/** 发布后健康评估结论。 */
export type HealthStatus = "HEALTHY" | "WARN" | "CRITICAL" | "UNKNOWN";

/** 健康规则的单条执行结果。 */
export type HealthRuleStatus = "PASSED" | "WARNED" | "FAILED" | "UNKNOWN" | "SKIPPED";

/** 健康规则所属质量维度。 */
export type HealthDimension =
  | "FRESHNESS"
  | "COMPLETENESS"
  | "VALIDITY"
  | "UNIQUENESS"
  | "CONSISTENCY"
  | "IDENTITY"
  | "SCHEMA"
  | "TEMPORAL";

/** 主动健康检查的批次状态。 */
export type HealthCheckStatus =
  | "QUEUED"
  | "RUNNING"
  | "SUCCEEDED"
  | "PARTIAL"
  | "FAILED"
  | "CANCELLED"
  | "REJECTED";

/** 主动健康检查中单个 target 的状态。 */
export type HealthCheckTargetStatus =
  | "QUEUED"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED"
  | "REJECTED";

/** service-api 本地 outbox 投递状态。 */
export type DeliveryStatus = "PENDING" | "DELIVERING" | "ACCEPTED" | "REJECTED" | "DEAD_LETTER";

/** 操作记录可见的投递状态，系统来源不经过公开提交。 */
export type OperationDeliveryStatus = DeliveryStatus | "NOT_APPLICABLE";

/** 用户动作与权威资源分离后的动作结果。 */
export type OperationResult =
  | "UNKNOWN"
  | "QUEUED"
  | "RUNNING"
  | "CANCEL_REQUESTED"
  | "SUCCEEDED"
  | "PARTIAL"
  | "FAILED"
  | "CANCELLED"
  | "INTERRUPTED"
  | "SKIPPED"
  | "REJECTED";

/** 公开写入动作的固定枚举。 */
export type SubmissionAction =
  | "SYNC_SUBMIT"
  | "SYNC_CANCEL"
  | "SYNC_RETRY"
  | "HEALTH_CHECK_SUBMIT"
  | "SCHEDULE_UPSERT"
  | "SCHEDULE_SET_ENABLED";

/** 执行槽的可见状态。 */
export type ExecutionSlotState = "IDLE" | "RUNNING" | "RECOVERING";

/** 自动计划支持的频率类型。 */
export type ScheduleFrequencyKind = "TRADING_DAY" | "DAILY" | "WEEKLY" | "MONTHLY" | "INTERVAL";

/** 自动计划目标日期的服务端解析策略。 */
export type ScheduleDateResolution =
  | "NONE"
  | "SCHEDULED_LOCAL_DATE"
  | "LATEST_COMPLETED_TRADING_DATE";

/** 安全错误的固定阶段，消息已由服务端脱敏。 */
export type ErrorStage =
  | "PREFLIGHT"
  | "QUEUE"
  | "DELIVERY"
  | "PROVIDER_FETCH"
  | "NORMALIZE"
  | "QUALITY_GATE"
  | "HEALTH_EVALUATION"
  | "PERSIST"
  | "PUBLISH"
  | "CHECKPOINT"
  | "SCHEDULE"
  | "CANCEL"
  | "RECOVERY";

/** 公开响应中可展示的脱敏失败摘要。 */
export interface ErrorSummary {
  code: string;
  stage: ErrorStage;
  retryable: boolean;
  message: string;
}

/** Web 只消费的操作者显示投影，绝不包含内部 `actorRef`。 */
export interface ActorDisplay {
  actorType: "USER" | "SYSTEM";
  systemKind: "SCHEDULE" | "LEGACY" | "RECOVERY" | "OTHER" | null;
  actorId: string | null;
  displayName: string;
  deleted: boolean;
}

/** 数据集来源血缘中的供应商、真实上游与适配器信息。 */
export interface SourceBinding {
  providerId: string;
  upstreamSource: string;
  sourceDataset: string;
  adapterId: string;
  methodologyCode: string;
  methodologyVersion: number;
  approvalStatus: "RESEARCH" | "CANDIDATE" | "APPROVED" | "SUSPENDED" | "UNSUPPORTED";
  role: "PRIMARY" | "FALLBACK" | "SHADOW";
  effective: boolean;
}

/** 自动计划中冻结的目标日期解析策略。 */
export interface ScheduleTargetPolicy {
  policyVersion: number;
  dateResolution: ScheduleDateResolution;
}

/** 服务端提供的计划模式与目标解析策略选项。 */
export interface ScheduleTargetPolicyOption {
  mode: Exclude<SyncMode, "DATE_RANGE">;
  policy: ScheduleTargetPolicy;
  isDefault: boolean;
}

/** 全量数据集的固定业务范围，不携带任何 Provider 参数。 */
export interface GlobalTargetSelector {
  kind: "GLOBAL";
}

/** 一只 A 股证券的交易所与代码范围。 */
export interface InstrumentTargetSelector {
  kind: "INSTRUMENT";
  exchange: "SSE" | "SZSE" | "BSE";
  symbol: string;
}

/** 行业分类体系中的单个行业代码范围。 */
export interface SectorTargetSelector {
  kind: "SECTOR";
  scheme: string;
  sectorCode: string;
}

/** 行业或分类体系的整体范围。 */
export interface SchemeTargetSelector {
  kind: "SCHEME";
  scheme: string;
}

/** 单个证券交易所范围。 */
export interface ExchangeTargetSelector {
  kind: "EXCHANGE";
  exchange: "SSE" | "SZSE" | "BSE";
}

/** 单个期货交易场所及合约范围。 */
export interface ContractTargetSelector {
  kind: "CONTRACT";
  venue: "CFFEX" | "SHFE" | "DCE" | "CZCE" | "INE";
  contract: string;
}

/** 全量 ETF fan-out 在预检时冻结的沪深 profile publication。 */
export interface EtfProfileDataVersions {
  SSE: string;
  SZSE: string;
}

/** ETF 主数据保留既有单市场刷新形状。 */
export interface EtfMasterSingleVenueTargetSelector {
  kind: "ETF";
  operation: "MASTER";
  venue: "SSE" | "SZSE";
  etf: null;
}

/** ETF 主数据以显式沪深双市场范围驱动唯一自动计划。 */
export interface EtfMasterAllVenuesTargetSelector {
  kind: "ETF";
  operation: "MASTER";
  venue: null;
  scope: "ALL_VENUES";
  etf: null;
}

/** ETF 主数据支持双市场完整刷新，并保留单市场兼容请求。 */
export type EtfMasterTargetSelector =
  | EtfMasterSingleVenueTargetSelector
  | EtfMasterAllVenuesTargetSelector;

/** 单只 ETF 使用用户显式提供的 canonical identity，不从代码前缀推断分类。 */
export interface OneEtfTargetSelector {
  kind: "ETF";
  operation: "STATUS" | "BARS" | "NAV";
  venue: "SSE" | "SZSE" | null;
  etf: string;
}

/** 全量 ETF 预检草稿尚未绑定 profile publication。 */
export interface AllEtfsDraftTargetSelector {
  kind: "ETF";
  operation: "STATUS" | "BARS" | "NAV";
  venue: null;
  scope: "ALL_ETFS";
  etf: null;
  profileDataVersions: null;
}

/** 全量 ETF 预检结果与提交冻结沪深两市的 profile publication。 */
export interface AllEtfsFrozenTargetSelector {
  kind: "ETF";
  operation: "STATUS" | "BARS" | "NAV";
  venue: null;
  scope: "ALL_ETFS";
  etf: null;
  profileDataVersions: EtfProfileDataVersions;
}

/** ETF 编辑器和 schedule 模板只构造未冻结的 selector。 */
export type EtfTargetSelector =
  | EtfMasterTargetSelector
  | OneEtfTargetSelector
  | AllEtfsDraftTargetSelector;

/** 两融市场汇总和证券日明细只能按沪深市场批量执行，不能携带未实现的证券子选择器。 */
export interface MarginDailyTargetSelector {
  kind: "MARGIN";
  operation: "MARKET" | "SECURITY";
  venue: "SSE" | "SZSE";
  security: null;
}

/** 两融资格快照只支持深交所和北交所，北交所真实来源由该独立分支显式表达。 */
export interface MarginEligibilityTargetSelector {
  kind: "MARGIN";
  operation: "ELIGIBILITY";
  venue: "SZSE" | "BSE";
  security: null;
}

/** 两融数据集的严格市场级 selector 并集。 */
export type MarginTargetSelector = MarginDailyTargetSelector | MarginEligibilityTargetSelector;

/** 完整沪深港通数据包的通道和方向范围；不承载持仓等越界操作。 */
export interface StockConnectTargetSelector {
  kind: "STOCK_CONNECT";
  operation: "MARKET";
  channel: "ALL" | "SH" | "SZ";
  direction: "NORTHBOUND" | "SOUTHBOUND" | null;
}

/** 港通市场统计 `research` 独立于正式互联互通 `bundle`，不携带或暗示正式 `publication`。 */
export interface StockConnectResearchTargetSelector {
  kind: "STOCK_CONNECT_RESEARCH";
  operation: "MARKET_STAT";
  channel: "ALL" | "SH" | "SZ";
  direction: "NORTHBOUND" | "SOUTHBOUND" | null;
}

/** 龙虎榜或大宗交易的业务事件范围。 */
export interface TradingEventTargetSelector {
  kind: "TRADING_EVENT";
  operation: "DRAGON_TIGER" | "BLOCK_TRADE";
}

/** 指数目录快照不以单条指数作为同步范围。 */
export interface IndexCatalogTargetSelector {
  kind: "INDEX";
  administrator: "CSI" | "CNI";
  capability: "index.catalog.snapshot";
  indexCode: null;
}

/** 指数成分或权重快照必须以一个显式指数代码作为同步范围。 */
export interface IndexSnapshotTargetSelector {
  kind: "INDEX";
  administrator: "CSI" | "CNI";
  capability: "index.constituent.snapshot" | "index.weight.snapshot";
  indexCode: string;
}

/** 指数目录与单指数快照的严格受控范围。 */
export type IndexTargetSelector = IndexCatalogTargetSelector | IndexSnapshotTargetSelector;

/** 个股日频资金流必须显式携带沪深北交易所和六码证券代码。 */
export interface MoneyFlowDailyEquityTargetSelector {
  kind: "MONEY_FLOW";
  operation: "DAILY";
  scope: "EQUITY";
  exchange: "SSE" | "SZSE" | "BSE";
  symbol: string;
}

/** 东财行业日频资金流固定使用 `eastmoney.industry` 分类体系。 */
export interface MoneyFlowDailySectorTargetSelector {
  kind: "MONEY_FLOW";
  operation: "DAILY";
  scope: "SECTOR";
  scheme: "eastmoney.industry";
  sectorCode: string;
}

/** 全市场日频资金流不带证券、行业或方法学参数。 */
export interface MoneyFlowDailyMarketTargetSelector {
  kind: "MONEY_FLOW";
  operation: "DAILY";
  scope: "MARKET";
}

/** 东财按单笔大小排行的个股资金流窗口。 */
export interface MoneyFlowEastmoneyEquityRankingTargetSelector {
  kind: "MONEY_FLOW";
  operation: "RANKING";
  methodology: "EASTMONEY_ORDER_SIZE";
  scope: "EQUITY";
  window: "TODAY" | "DAY_3" | "DAY_5" | "DAY_10";
}

/** 东财行业、概念或地域排行必须显式指明可用的 sectorType。 */
export interface MoneyFlowEastmoneySectorRankingTargetSelector {
  kind: "MONEY_FLOW";
  operation: "RANKING";
  methodology: "EASTMONEY_ORDER_SIZE";
  scope: "SECTOR";
  sectorType: "INDUSTRY" | "CONCEPT" | "REGION";
  window: "TODAY" | "DAY_5" | "DAY_10";
}

/** 同花顺按交易方向排行仅覆盖个股、行业和概念三个聚合范围。 */
export interface MoneyFlowThsRankingTargetSelector {
  kind: "MONEY_FLOW";
  operation: "RANKING";
  methodology: "THS_TRADE_DIRECTION";
  scope: "EQUITY" | "INDUSTRY" | "CONCEPT";
  window: "INTRADAY" | "DAY_3" | "DAY_5" | "DAY_10" | "DAY_20";
}

/** 日频与排行资金流的严格可消费同步范围。 */
export type MoneyFlowTargetSelector =
  | MoneyFlowDailyEquityTargetSelector
  | MoneyFlowDailySectorTargetSelector
  | MoneyFlowDailyMarketTargetSelector
  | MoneyFlowEastmoneyEquityRankingTargetSelector
  | MoneyFlowEastmoneySectorRankingTargetSelector
  | MoneyFlowThsRankingTargetSelector;

/** 合同定义的严格 selector 并集，禁止承载任意 JSON、URI 或凭据。 */
export type TargetSelector =
  | GlobalTargetSelector
  | InstrumentTargetSelector
  | SectorTargetSelector
  | SchemeTargetSelector
  | ExchangeTargetSelector
  | ContractTargetSelector
  | EtfTargetSelector
  | MarginTargetSelector
  | StockConnectTargetSelector
  | StockConnectResearchTargetSelector
  | TradingEventTargetSelector
  | IndexTargetSelector
  | MoneyFlowTargetSelector;

/** 预检返回与运行详情可携带服务端冻结后的全量 ETF selector。 */
export type FrozenTargetSelector =
  | Exclude<TargetSelector, AllEtfsDraftTargetSelector>
  | AllEtfsFrozenTargetSelector;

/** 所有公开读取可见的 selector，包括草稿模板与预检冻结快照。 */
export type RuntimeTargetSelector = TargetSelector | FrozenTargetSelector;

/** 服务端为一个数据集声明的手工和自动执行能力。 */
export interface DatasetCapability {
  supportedModes: SyncMode[];
  scheduleSupportedModes: Exclude<SyncMode, "DATE_RANGE">[];
  scheduleTargetPolicyOptions: ScheduleTargetPolicyOption[];
  selectorKinds: TargetSelectorKind[];
  maxRangeDays: number | null;
  scheduleEligible: boolean;
  manualEnabled: boolean;
  correctionLookbackDays: number;
}

/** 服务端按版本化 policy 计算的时间与新鲜度投影。 */
export interface DatasetTiming {
  lastAttemptStartedAt: string | null;
  lastAttemptFinishedAt: string | null;
  lastAttemptStatus: RunStatus | null;
  lastSuccessAt: string | null;
  lastPublishedAt: string | null;
  dataAsOf: string | null;
  dataAsOfKind:
    | "TRADING_DATE"
    | "REPORT_PERIOD"
    | "OBSERVATION_DATE"
    | "SNAPSHOT_DATE"
    | "EVENT_DATE"
    | "NOT_APPLICABLE";
  dataAsOfLabel: string;
  coverageFrom: string | null;
  coverageTo: string | null;
  freshnessStatus: FreshnessStatus;
  freshnessLagValue: number | null;
  freshnessLagUnit: "MINUTES" | "CALENDAR_DAYS" | "TRADING_DAYS" | "REPORTING_PERIODS" | null;
  freshnessReasonCode: string | null;
  freshnessEvaluatedAt: string;
}

/** 同步子运行摘要，供目录、队列与命令详情共同使用。 */
export interface RunSummary {
  runId: string;
  commandId: string;
  datasetCode: string;
  mode: SyncMode;
  status: RunStatus;
  queuePosition: number | null;
  requestedAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  progress: {
    completedPartitions: number;
    totalPartitions: number;
    processedRecords: number;
    estimatedRecords: number | null;
  };
  error: ErrorSummary | null;
}

/** 发布后健康概览，不替代不可变健康评估详情。 */
export interface HealthSummary {
  status: HealthStatus;
  score: number | null;
  evaluatedAt: string | null;
  evaluationId: string | null;
  warningCount: number;
  criticalCount: number;
  openIssueCount: number;
  affectedRecordCount: number | null;
}

/** 目录中可见的自动计划摘要。 */
export interface ScheduleSummary {
  scheduleId: string;
  enabled: boolean;
  frequency: ScheduleFrequency;
  nextRunAt: string | null;
  version: number;
}

/** 数据目录可展示的一条数据集摘要。 */
export interface DatasetSummary {
  datasetCode: string;
  displayName: string;
  domain: string;
  lifecycleStatus: "RESEARCH" | "CANDIDATE" | "PRODUCTION" | "RETIRED";
  availability: DatasetAvailability;
  availabilityReasonCode: string | null;
  observationState: DatasetObservationState;
  observationStateReasonCode: string | null;
  sourceBindings: SourceBinding[];
  capability: DatasetCapability;
  timing: DatasetTiming;
  latestRun: RunSummary | null;
  healthSummary: HealthSummary;
  scheduleSummary: ScheduleSummary | null;
}

/** 数据集详情可展示的新鲜度策略。 */
export interface FreshnessPolicy {
  timezone: string;
  calendarCode: string | null;
  warnAfterMinutes: number;
  criticalAfterMinutes: number;
}

/** 已发布版本的安全摘要。 */
export interface PublicationSummary {
  dataVersion: string;
  releaseId: string;
  publishedAt: string;
  rowCount: number;
}

/** 数据集注册的健康规则摘要。 */
export interface HealthRuleSummary {
  ruleCode: string;
  dimension: HealthDimension;
  severity: "INFO" | "WARN" | "CRITICAL";
  version: number;
}

/** 数据集详情的完整公开投影。 */
export interface DatasetDetail {
  summary: DatasetSummary;
  description: string;
  grain: string;
  freshnessPolicy: FreshnessPolicy | null;
  latestPublication: PublicationSummary | null;
  latestError: ErrorSummary | null;
  healthRules: HealthRuleSummary[];
}

/** 数据运维执行槽的安全状态投影。 */
export interface ExecutionSlot {
  state: ExecutionSlotState;
  runId: string | null;
  datasetCode: string | null;
  leaseUntil: string | null;
  heartbeatAt: string | null;
}

/** data-sync 公开总览中的跨服务安全字段。 */
export interface DataSyncOperationsOverview {
  datasetCount: number;
  enabledDatasetCount: number;
  healthSummary: HealthSummary;
  executionSlot: ExecutionSlot;
  queuedRunCount: number;
  failedRunCount24h: number;
  generatedAt: string;
}

/** service-api 对 data-sync 总览补充的 outbox 状态。 */
export interface OperationsOverview {
  dataSync: DataSyncOperationsOverview;
  deliveryPendingCount: number;
  deliveryDeadLetterCount: number;
}

/** 公开同步 target，日期字段必须与模式成对匹配。 */
export interface SyncTarget<TSelector extends RuntimeTargetSelector = RuntimeTargetSelector> {
  datasetCode: string;
  mode: SyncMode;
  selector: TSelector;
  dateFrom: string | null;
  dateTo: string | null;
  observationDate: string | null;
}

/** 浏览器送入 preflight 的同步草稿，禁止携带冻结 publication。 */
export type SyncPreflightTarget = SyncTarget<TargetSelector>;

/** preflight 返回与 submit 复用的同步快照。 */
export type SyncFrozenTarget = SyncTarget<FrozenTargetSelector>;

/** 预检对每个目标返回的无副作用估算。 */
export interface PreflightTargetResult {
  target: SyncFrozenTarget;
  eligible: boolean;
  estimatedPartitions: number;
  estimatedProviderCalls: number;
  resolvedDateFrom: string | null;
  resolvedDateTo: string | null;
  warnings: string[];
}

/** 同步预检的快照结果，不保证之后的队列位置或能力仍有效。 */
export interface SyncPreflight {
  preflightId: string;
  requestHash: string;
  expiresAt: string;
  queueDepth: number;
  executionSlot: ExecutionSlot;
  targets: PreflightTargetResult[];
  accepted: boolean;
}

/** 公开命令动作明确指定整批或单 run 作用域。 */
export interface CommandActionTarget {
  resourceType: "COMMAND" | "RUN";
  resourceId: string;
}

/** 命令详情是批量 child run 的唯一恢复来源。 */
export interface CommandDetailView {
  commandId: string;
  submissionId: string | null;
  status: CommandStatus;
  requestedAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  childRuns: RunSummary[];
  requestedBy: ActorDisplay;
  error: ErrorSummary | null;
}

/** 公开运行详情中的发布前质量门摘要。 */
export interface QualityGateSummary {
  disposition: "NOT_EVALUATED" | "PASSED" | "WARNED" | "BLOCKED";
  policyCode: string | null;
  policyVersion: number | null;
  affectedCount: number | null;
  error: ErrorSummary | null;
}

/** 运行详情的公开分区投影，不包含 checkpoint。 */
export interface RunPartitionView {
  partitionKey: string;
  status: RunStatus;
  attempt: number;
  error: ErrorSummary | null;
}

/** 运行时间线的公开操作者投影。 */
export interface OperationEventView {
  eventId: string;
  resourceType: DataOperationResourceType;
  resourceId: string;
  action: string;
  result:
    | "ACCEPTED"
    | "QUEUED"
    | "STARTED"
    | "CANCEL_REQUESTED"
    | "SUCCEEDED"
    | "PARTIAL"
    | "FAILED"
    | "CANCELLED"
    | "INTERRUPTED"
    | "SKIPPED"
    | "REJECTED";
  actor: ActorDisplay;
  requestId: string;
  occurredAt: string;
  error: ErrorSummary | null;
}

/** 运行详情的公开安全投影。 */
export interface RunDetail {
  run: RunSummary;
  target: SyncFrozenTarget;
  sourceSnapshot: SourceBinding[];
  qualityGate: QualityGateSummary;
  partitionCount: number;
  partitions: RunPartitionView[];
  partitionsNextCursor: string | null;
  timelineEventCount: number;
  timeline: OperationEventView[];
  timelineNextCursor: string | null;
  requestedBy: ActorDisplay;
}

/** 健康评估的不可变摘要与当前问题数。 */
export interface HealthEvaluationSummary {
  evaluationId: string;
  healthCheckId: string | null;
  datasetCode: string;
  dataVersion: string;
  releaseId: string;
  policyCode: string;
  policyVersion: number;
  status: HealthStatus;
  score: number | null;
  evaluatedAt: string;
  warningCount: number;
  criticalCount: number;
  currentOpenIssueCount: number;
  issueProjectionAsOf: string;
  affectedRecordCount: number | null;
}

/** 不可变健康评估中的一条规则事实。 */
export interface HealthRuleResult {
  ruleCode: string;
  dimension: HealthDimension;
  severity: "INFO" | "WARN" | "CRITICAL";
  status: HealthRuleStatus;
  expected: string | null;
  observed: string | null;
  affectedCount: number | null;
  sampleSummary: string | null;
  message: string;
}

/** 绑定 release 的不可变健康评估事实。 */
export interface HealthEvaluation {
  evaluationId: string;
  healthCheckId: string | null;
  datasetCode: string;
  dataVersion: string;
  releaseId: string;
  policyCode: string;
  policyVersion: number;
  status: HealthStatus;
  score: number | null;
  evaluatedAt: string;
  results: HealthRuleResult[];
}

/** 查询时的当前开放问题投影。 */
export interface HealthIssueSummary {
  issueId: string;
  ruleCode: string;
  dimension: HealthDimension;
  severity: "WARN" | "CRITICAL";
  status: "OPEN" | "ACKNOWLEDGED";
  firstDetectedAt: string;
  lastDetectedAt: string;
  affectedCount: number | null;
  evidenceSummary: string | null;
}

/** 不可变评估与可变问题投影的组合详情。 */
export interface HealthDetail {
  evaluation: HealthEvaluation;
  currentOpenIssueCount: number;
  currentOpenIssues: HealthIssueSummary[];
  currentOpenIssuesNextCursor: string | null;
  issueProjectionAsOf: string;
}

/** 主动健康检查的单个固定 target。 */
export interface HealthCheckTarget {
  datasetCode: string;
  dataVersion: string | null;
}

/** 主动健康检查在原提交顺序上的单 target 结果。 */
export interface HealthCheckTargetResult {
  target: HealthCheckTarget;
  resolvedDataVersion: string | null;
  status: HealthCheckTargetStatus;
  evaluationId: string | null;
  error: ErrorSummary | null;
}

/** 主动健康检查批次详情，target 顺序不能由客户端重建。 */
export interface HealthCheckDetailView {
  healthCheckId: string;
  submissionId: string | null;
  status: HealthCheckStatus;
  requestedAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  requestedBy: ActorDisplay;
  targets: HealthCheckTargetResult[];
  error: ErrorSummary | null;
}

/** 自动计划使用的结构化频率，禁止浏览器发送任意 cron。 */
export interface ScheduleFrequency {
  kind: ScheduleFrequencyKind;
  timezone: string;
  localTime: string | null;
  dayOfWeek: number | null;
  dayOfMonth: number | null;
  intervalMinutes: number | null;
  calendarCode: string | null;
}

/** 自动计划公开展示视图。 */
export interface ScheduleView {
  scheduleId: string;
  datasetCode: string;
  mode: Exclude<SyncMode, "DATE_RANGE">;
  selector: TargetSelector;
  targetPolicy: ScheduleTargetPolicy;
  enabled: boolean;
  frequency: ScheduleFrequency;
  misfirePolicy: "SKIP" | "RUN_ONCE";
  coalesce: boolean;
  nextRunAt: string | null;
  nextOccurrences: string[];
  version: number;
  updatedAt: string;
  updatedBy: ActorDisplay;
}

/** API 持久化一次用户写意图后的公开回执。 */
export interface SubmissionReceipt {
  submissionId: string;
  action: SubmissionAction;
  deliveryStatus: DeliveryStatus;
  operationResult: OperationResult;
  authorityResource: {
    resourceType: DataOperationResourceType;
    resourceId: string;
  } | null;
  queuePosition: number | null;
  authorizedAt: string;
  updatedAt: string;
  requestId: string;
  error: ErrorSummary | null;
}

/** 操作记录中用户或系统来源的一行公开投影。 */
export interface OperationRecord {
  submissionId: string | null;
  action: string;
  targetSummary: string;
  actor: ActorDisplay;
  reason: string;
  deliveryStatus: OperationDeliveryStatus;
  operationResult: OperationResult;
  authorityResource: {
    resourceType: DataOperationResourceType;
    resourceId: string;
  } | null;
  authorizedAt: string;
  completedAt: string | null;
  lastObservedAt: string | null;
  requestId: string;
  error: ErrorSummary | null;
}

/** 所有目录、运行、健康、计划与操作搜索共享的 cursor 页参数。 */
export interface CursorPageRequest {
  cursor?: string | null;
  limit?: number;
}

/** 数据资产检索的公开筛选条件。 */
export interface DatasetSearchRequest extends CursorPageRequest {
  query?: string | null;
  domains?: string[];
  providers?: string[];
  upstreamSources?: string[];
  availability?: DatasetAvailability[];
  observationStates?: DatasetObservationState[];
  runStatuses?: RunStatus[];
  healthStatuses?: HealthStatus[];
}

/** 数据资产搜索结果页。 */
export interface DatasetPage {
  items: DatasetSummary[];
  nextCursor: string | null;
  totalEstimate: number;
  generatedAt: string;
}

/** 同步预检请求。 */
export interface SyncPreflightRequest {
  targets: SyncPreflightTarget[];
}

/** 同步提交请求，必须引用同一份 preflight 快照。 */
export interface SyncSubmitRequest {
  preflightId: string;
  requestHash: string;
  targets: SyncFrozenTarget[];
  reason: string;
}

/** 取消或重试请求。 */
export interface CommandActionRequest {
  target: CommandActionTarget;
  reason: string;
}

/** 运行检索条件。 */
export interface RunSearchRequest extends CursorPageRequest {
  datasetCodes?: string[];
  statuses?: RunStatus[];
  requestedFrom?: string | null;
  requestedTo?: string | null;
}

/** 运行结果 cursor 页。 */
export interface RunPage {
  items: RunSummary[];
  nextCursor: string | null;
}

/** 运行详情请求的独立 partition 与 timeline cursor。 */
export interface RunDetailRequest {
  runId: string;
  partitionsCursor?: string | null;
  partitionsLimit?: number;
  timelineCursor?: string | null;
  timelineLimit?: number;
}

/** 健康评估检索条件。 */
export interface HealthSearchRequest extends CursorPageRequest {
  datasetCodes?: string[];
  statuses?: HealthStatus[];
  evaluatedFrom?: string | null;
  evaluatedTo?: string | null;
}

/** 健康评估结果页。 */
export interface HealthPage {
  items: HealthEvaluationSummary[];
  nextCursor: string | null;
}

/** 健康详情请求的独立开放问题 cursor。 */
export interface HealthDetailRequest {
  evaluationId: string;
  issuesCursor?: string | null;
  issuesLimit?: number;
}

/** 主动健康检查提交请求。 */
export interface HealthCheckSubmitRequest {
  targets: HealthCheckTarget[];
  reason: string;
}

/** 自动计划检索条件。 */
export interface ScheduleSearchRequest extends CursorPageRequest {
  datasetCodes?: string[];
  enabled?: boolean | null;
}

/** 自动计划创建或乐观锁更新请求。 */
export interface ScheduleUpsertRequest {
  scheduleId: string | null;
  datasetCode: string;
  mode: Exclude<SyncMode, "DATE_RANGE">;
  selector: TargetSelector;
  targetPolicy: ScheduleTargetPolicy;
  frequency: ScheduleFrequency;
  misfirePolicy: "SKIP" | "RUN_ONCE";
  coalesce: boolean;
  enabled: boolean;
  expectedVersion: number | null;
  reason: string;
}

/** 自动计划启停请求。 */
export interface ScheduleEnabledRequest {
  scheduleId: string;
  enabled: boolean;
  expectedVersion: number;
  reason: string;
}

/** 自动计划结果 cursor 页。 */
export interface SchedulePage {
  items: ScheduleView[];
  nextCursor: string | null;
}

/** 公开操作记录检索条件。 */
export interface OperationSearchRequest extends CursorPageRequest {
  actorIds?: string[];
  actions?: string[];
  deliveryStatuses?: OperationDeliveryStatus[];
  operationResults?: OperationResult[];
  occurredFrom?: string | null;
  occurredTo?: string | null;
}

/** 公开操作记录结果 cursor 页。 */
export interface OperationPage {
  items: OperationRecord[];
  nextCursor: string | null;
}
