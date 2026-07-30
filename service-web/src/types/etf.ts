/** ETF 数据集固定代码，调用方不得用自由字符串访问其他数据集。 */
export type EtfDatasetCode =
  | "fund.etf.profile.reported"
  | "fund.etf.bar.1d.reported"
  | "fund.etf.nav.1d.reported"
  | "fund.etf.trading_state.reported";

/** ETF 上市场所由产品目录明确给出，不能根据证券代码前缀猜测。 */
export type EtfExchange = "SSE" | "SZSE";

/** ETF 上市生命周期状态；`UNKNOWN` 表示来源没有给出可靠结论。 */
export type EtfListingStatus = "LISTED" | "SUSPENDED" | "DELISTED" | "UNKNOWN";

/** ETF NAV 的来源口径。 */
export type EtfNavKind = "UNIT" | "ACCUMULATED";

/** ETF NAV 的来源终态。 */
export type EtfNavFinality = "FINAL" | "PROVISIONAL" | "UNKNOWN";

/** ETF 交易、申购和赎回三个互不推导的状态维度。 */
export type EtfStateDimension = "TRADING" | "SUBSCRIPTION" | "REDEMPTION";

/** ETF 产品目录 v2 的公开业务字段。 */
export interface EtfProfileValues {
  etfEntityRef: string;
  exchange: EtfExchange;
  symbol: string;
  displayName: string;
  etfType: string;
  managementMode: string;
  managerName: string | null;
  custodianName: string | null;
  listedOn: string | null;
  delistedOn: string | null;
  listingStatus: EtfListingStatus;
  quoteCurrency: string;
  navCurrency: string;
  sourceTimePrecision: string;
}

/** ETF 未复权日线 v2 的公开业务字段；价格和金额保留十进制字符串精度。 */
export interface EtfDailyBarValues {
  tradeDate: string;
  etfEntityRef: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  volumeUnit: string;
  amount: string;
  currency: string;
  tradeStatus: string | null;
  adjustment: "UNADJUSTED";
}

/** ETF NAV 日值 v2 的公开业务字段；`nav` 保留十进制字符串精度。 */
export interface EtfNavValues {
  navDate: string;
  etfEntityRef: string;
  navKind: EtfNavKind;
  nav: string;
  currency: string;
  finality: EtfNavFinality;
}

/** ETF 日级状态 v2 的公开业务字段，缺少某个维度时不能从其他维度补算。 */
export interface EtfTradingStateValues {
  etfEntityRef: string;
  stateDimension: EtfStateDimension;
  state: string;
  effectiveFrom: string;
  effectiveTo: string | null;
  reason: string | null;
}

/** typed market-data 记录中的公开实体投影。 */
export interface MarketDataRecordEntity {
  entityRef: string;
  entityType: string;
  identifiers: readonly unknown[];
}

/** typed market-data 的标准记录 envelope，业务字段始终位于 `values`。 */
export interface MarketDataRecord<TValues> {
  recordRef: string;
  recordType: string;
  entity: MarketDataRecordEntity;
  time: Readonly<Record<string, unknown>>;
  publicUsableAt: string;
  availabilityBasis: string;
  sourcePublishedAt: string | null;
  observedAt: string;
  dataVersion: string;
  sourceRef: string;
  methodologyVersion: string;
  qualityStatus: string;
  revision: {
    revisionNumber: number;
    currentInPublication: boolean;
  };
  values: TValues;
}

/** 已发布数据集公开的来源信息，不包含供应商凭据或 raw 对象位置。 */
export interface MarketDataReleaseSource {
  sourceRef: string;
  publisher: string;
  sourceDataset: string;
  authoritative: boolean;
  redistribution: string;
  coverageNote: string | null;
}

/** 有可读 publication 时的不可变发布元数据。 */
export interface MarketDataAvailableRelease {
  dataVersion: string;
  publishedAt: string;
  knowledgeCutoff: string;
  publicUsableAt: string;
  effectiveFrom: string | null;
  effectiveTo: string | null;
  methodology: Readonly<Record<string, unknown>>;
  sources: readonly MarketDataReleaseSource[];
  quality: Readonly<Record<string, unknown>>;
  completeness: "COMPLETE" | "PARTIAL" | "UNKNOWN";
}

/** 无 publication 或来源不可用时的成功空结果元数据。 */
export type MarketDataEmptyReasonCode =
  | "NO_MATCHING_FACTS"
  | "PROVIDER_UNAVAILABLE"
  | "CAPABILITY_NOT_CONFIGURED"
  | "PUBLICATION_NOT_AVAILABLE"
  | "NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET";

/** 无 publication、无匹配记录或当前口径不支持时的严格非数据结果。 */
export interface MarketDataEmptyRelease {
  state: "EMPTY" | "SOURCE_UNAVAILABLE" | "CURRENTLY_UNSUPPORTED";
  observedAt: string | null;
  reasonCode: MarketDataEmptyReasonCode;
}

/** typed market-data 查询页的固定元数据。 */
export interface MarketDataPageMeta {
  requestId: string;
  contractVersion: "1.0.0";
  dataset: {
    code: EtfDatasetCode;
    schemaVersion: 2;
  };
  availability: "AVAILABLE" | "EMPTY" | "SOURCE_UNAVAILABLE" | "CURRENTLY_UNSUPPORTED";
  release: MarketDataAvailableRelease | MarketDataEmptyRelease;
  visibility: Readonly<Record<string, unknown>>;
  page: {
    limit: number;
    hasMore: boolean;
    nextCursor: string | null;
  };
  coverage: Readonly<Record<string, unknown>>;
  warnings: readonly string[];
  disclaimers: readonly string[];
}

/** 一个 ETF typed dataset 的已校验查询结果页。 */
export interface MarketDataPage<TValues> {
  meta: MarketDataPageMeta;
  records: readonly MarketDataRecord<TValues>[];
}

/** ETF 列表 URL 与远程查询共同使用的稳定筛选状态。 */
export interface EtfListFilters {
  exchange: EtfExchange;
  q?: string;
  sort: "symbol" | "displayName";
  order: "asc" | "desc";
  cursor?: string;
  page: number;
  pageSize: number;
}
