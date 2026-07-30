import type {
  EtfExchange,
  EtfListingStatus,
  EtfNavFinality,
  EtfStateDimension,
  MarketDataAvailableRelease,
  MarketDataPageMeta,
} from "../types/etf";

/** 一项可参与 publication 日期一致性检查的数据集。 */
export interface PublicationCandidate {
  label: string;
  meta: MarketDataPageMeta | undefined;
}

/** ETF 页面把 typed reader 的可用性收敛为四个互斥展示状态。 */
export type EtfAvailabilityState =
  | "available"
  | "empty"
  | "source-unavailable"
  | "currently-unsupported";

/** 保留合法空结果、来源故障与口径暂不支持的区别。 */
export function etfAvailabilityState(
  availability: MarketDataPageMeta["availability"],
): EtfAvailabilityState {
  if (availability === "AVAILABLE") return "available";
  if (availability === "EMPTY") return "empty";
  return availability === "SOURCE_UNAVAILABLE" ? "source-unavailable" : "currently-unsupported";
}

/** 将交易所固定代码转换为中文标签。 */
export function etfExchangeLabel(exchange: EtfExchange): string {
  return exchange === "SSE" ? "上海证券交易所" : "深圳证券交易所";
}

/** 将上市状态转换为不夸大来源结论的中文标签。 */
export function etfListingStatusLabel(status: EtfListingStatus): string {
  const labels: Record<EtfListingStatus, string> = {
    LISTED: "上市",
    SUSPENDED: "暂停上市",
    DELISTED: "已退市",
    UNKNOWN: "状态未披露",
  };

  return labels[status];
}

/** 将 ETF 独立状态维度转换为中文标签。 */
export function etfStateDimensionLabel(dimension: EtfStateDimension): string {
  const labels: Record<EtfStateDimension, string> = {
    TRADING: "交易状态",
    SUBSCRIPTION: "申购状态",
    REDEMPTION: "赎回状态",
  };

  return labels[dimension];
}

/** 将 NAV 终态转换为中文标签。 */
export function etfNavFinalityLabel(finality: EtfNavFinality): string {
  const labels: Record<EtfNavFinality, string> = {
    FINAL: "正式值",
    PROVISIONAL: "暂定值",
    UNKNOWN: "终态未披露",
  };

  return labels[finality];
}

/** 格式化来源业务日期；空值明确显示未披露。 */
export function formatEtfDate(value: string | null): string {
  return value ?? "未披露";
}

/** 以中国市场时区格式化 publication 时间。 */
export function formatPublicationTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

/** 判断 release 是否包含可读取 publication。 */
export function isAvailableRelease(
  release: MarketDataPageMeta["release"],
): release is MarketDataAvailableRelease {
  return "dataVersion" in release;
}

/** 返回公开来源名称，多个来源并列且不暴露技术适配器名称。 */
export function releasePublisherLabel(meta: MarketDataPageMeta): string {
  if (!isAvailableRelease(meta.release)) {
    return "无可用 publication";
  }
  const publishers = Array.from(
    new Set(
      meta.release.sources.map(
        /** 只读取来源合同允许公开的 publisher。 */
        (source) => source.publisher,
      ),
    ),
  );

  return publishers.length > 0 ? publishers.join("、") : "来源未披露";
}

/** 将公开 warning code 聚合为不会猜测口径的延迟提示。 */
export function releaseWarningLabel(meta: MarketDataPageMeta): string | null {
  return meta.warnings.length > 0 ? `数据提示：${meta.warnings.join("、")}` : null;
}

/** 汇总无 publication 或来源不可用的原因、最近观测时间与同步警告。 */
export function unavailableReleaseSummary(meta: MarketDataPageMeta): string {
  if (isAvailableRelease(meta.release)) {
    return releaseWarningLabel(meta) ?? "当前 publication 可用";
  }

  const observedAt =
    meta.release.observedAt === null
      ? "未记录最近观测时间"
      : `最近观测 ${formatPublicationTime(meta.release.observedAt)}`;
  const warnings = meta.warnings.length === 0 ? "" : `；同步提示：${meta.warnings.join("、")}`;

  return `${meta.release.reasonCode}；${observedAt}${warnings}`;
}

/** 将 RFC3339 publication 时间转换为固定的上海日历日。 */
export function publicationShanghaiDate(value: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(value));
  let year = "";
  let month = "";
  let day = "";

  for (const part of parts) {
    if (part.type === "year") year = part.value;
    if (part.type === "month") month = part.value;
    if (part.type === "day") day = part.value;
  }

  return `${year}-${month}-${day}`;
}

/** 按上海日历日比较各 publication，返回独立发布提示而不强制拼成同一快照。 */
export function publicationDateMismatch(
  candidates: readonly PublicationCandidate[],
): string | null {
  const visible = candidates.flatMap(
    /** 只让已经有 publication 的数据集参与对比。 */
    (candidate) => {
      const release = candidate.meta?.release;
      return release !== undefined && isAvailableRelease(release)
        ? [{ label: candidate.label, publishedAt: release.publishedAt }]
        : [];
    },
  );
  const dates = new Set(
    visible.map(
      /** 页面明确采用中国市场时区，跨 UTC 日界时也必须保持上海日期。 */
      (candidate) => publicationShanghaiDate(candidate.publishedAt),
    ),
  );
  if (dates.size <= 1) {
    return null;
  }

  return `各数据集独立发布：${visible
    .map(
      /** 保留每个数据集自己的 publication 日期，避免暗示同批次。 */
      (candidate) => `${candidate.label} ${publicationShanghaiDate(candidate.publishedAt)}`,
    )
    .join("；")}`;
}
