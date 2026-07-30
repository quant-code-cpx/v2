import type {
  StockConnectChannelCode,
  StockConnectDateSelection,
  StockConnectRanking,
} from "../../../types/stock-connect";

/** 枚举可出现在路由 path 或 URL channel 参数中的稳定通道短名。 */
export const stockConnectChannelSlugs = [
  "sh-northbound",
  "sz-northbound",
  "sh-southbound",
  "sz-southbound",
] as const;

/** 表示一个可分享的通道路由短名。 */
export type StockConnectChannelSlug = (typeof stockConnectChannelSlugs)[number];

/** 枚举总览页支持的业务方向筛选。 */
export const stockConnectDirections = ["all", "northbound", "southbound"] as const;

/** 表示总览可分享的业务方向。 */
export type StockConnectDirectionFilter = (typeof stockConnectDirections)[number];

/** 枚举 URL 支持的榜单短名。 */
export const stockConnectRankingSlugs = ["active", "net-buy", "net-sell"] as const;

/** 表示 URL 中的来源榜或榜内净额排序。 */
export type StockConnectRankingSlug = (typeof stockConnectRankingSlugs)[number];

/** 枚举允许的交易日趋势窗口。 */
export const stockConnectTrendDayOptions = [20, 60, 120, 250] as const;

/** 表示可分享的交易日趋势窗口。 */
export type StockConnectTrendDays = (typeof stockConnectTrendDayOptions)[number];

/** 枚举允许的活跃榜分页大小。 */
export const stockConnectPageSizeOptions = [20, 50, 100] as const;

/** 表示可分享的来源活跃榜分页大小。 */
export type StockConnectPageSize = (typeof stockConnectPageSizeOptions)[number];

/** 表示经过公历校验的精确 ISO 日期文本。 */
export type StockConnectExactDate = `${number}-${number}-${number}`;

/** 表示 URL 中 latest 语义或经过校验的精确交易日。 */
export type StockConnectDateUrlValue = "latest" | StockConnectExactDate;

/** 描述总览和通道详情由 URL 持有的全部筛选状态。 */
export interface StockConnectUrlState {
  date: StockConnectDateUrlValue;
  direction: StockConnectDirectionFilter;
  channel: StockConnectChannelSlug;
  ranking: StockConnectRankingSlug;
  trendDays: StockConnectTrendDays;
  pageSize: StockConnectPageSize;
  cursor?: string;
}

/** 描述证券上下文页由 URL 持有的日期、通道和历史窗口。 */
export interface StockConnectSecurityUrlState {
  date: StockConnectDateUrlValue;
  channel?: StockConnectChannelSlug;
  trendDays: StockConnectTrendDays;
}

/** 固定通道短名与公开合同代码的双向映射。 */
export const stockConnectChannelCodeBySlug: Record<
  StockConnectChannelSlug,
  StockConnectChannelCode
> = {
  "sh-northbound": "SH_NORTHBOUND",
  "sz-northbound": "SZ_NORTHBOUND",
  "sh-southbound": "SH_SOUTHBOUND",
  "sz-southbound": "SZ_SOUTHBOUND",
};

/** 固定公开合同代码与通道短名的双向映射。 */
export const stockConnectChannelSlugByCode: Record<
  StockConnectChannelCode,
  StockConnectChannelSlug
> = {
  SH_NORTHBOUND: "sh-northbound",
  SZ_NORTHBOUND: "sz-northbound",
  SH_SOUTHBOUND: "sh-southbound",
  SZ_SOUTHBOUND: "sz-southbound",
};

/** 固定 URL 榜单短名与公开合同枚举的映射。 */
export const stockConnectRankingBySlug: Record<StockConnectRankingSlug, StockConnectRanking> = {
  active: "SOURCE_ACTIVE",
  "net-buy": "NET_BUY",
  "net-sell": "NET_SELL",
};

/** 判断精确日期是否为真实存在的 ISO 公历日期。 */
export function isExactTradeDate(value: string): value is StockConnectExactDate {
  const matched = /^(\d{4})-(\d{2})-(\d{2})$/u.exec(value);
  if (matched === null) {
    return false;
  }

  const year = Number(matched[1]);
  const month = Number(matched[2]);
  const day = Number(matched[3]);
  const parsed = new Date(Date.UTC(year, month - 1, day));

  return (
    parsed.getUTCFullYear() === year &&
    parsed.getUTCMonth() === month - 1 &&
    parsed.getUTCDate() === day
  );
}

/** 把 URL 日期选择转换为公开合同的互斥对象。 */
export function toStockConnectDateSelection(
  value: StockConnectUrlState["date"],
): StockConnectDateSelection {
  return value === "latest"
    ? { mode: "LATEST", exactDate: null }
    : { mode: "EXACT", exactDate: value };
}

/** 把方向筛选转换为明确通道集合，不进行金额聚合。 */
export function stockConnectChannelsForDirection(
  direction: StockConnectDirectionFilter,
): StockConnectChannelCode[] {
  if (direction === "northbound") {
    return ["SH_NORTHBOUND", "SZ_NORTHBOUND"];
  }
  if (direction === "southbound") {
    return ["SH_SOUTHBOUND", "SZ_SOUTHBOUND"];
  }

  return ["SH_NORTHBOUND", "SZ_NORTHBOUND", "SH_SOUTHBOUND", "SZ_SOUTHBOUND"];
}

/** 从未知 URL 值中读取受支持枚举，否则返回给定默认值。 */
function enumOrDefault<T extends string>(
  value: string | null,
  options: readonly T[],
  fallback: T,
): T {
  return options.includes(value as T) ? (value as T) : fallback;
}

/** 从 URL 读取趋势窗口并拒绝任意数字。 */
function parseTrendDays(value: string | null): StockConnectTrendDays {
  const parsed = Number(value);
  return stockConnectTrendDayOptions.includes(parsed as StockConnectTrendDays)
    ? (parsed as StockConnectTrendDays)
    : 20;
}

/** 从 URL 读取分页大小并拒绝任意数字。 */
function parsePageSize(value: string | null): StockConnectPageSize {
  const parsed = Number(value);
  return stockConnectPageSizeOptions.includes(parsed as StockConnectPageSize)
    ? (parsed as StockConnectPageSize)
    : 20;
}

/** 从 URL 读取 latest 或精确交易日，不把其他时间文本当作日期。 */
function parseDate(value: string | null): StockConnectUrlState["date"] {
  if (value === null || value === "latest") {
    return "latest";
  }

  return isExactTradeDate(value) ? value : "latest";
}

/** 从 URL 读取并规范化总览或通道详情筛选。 */
export function parseStockConnectUrlState(searchParameters: URLSearchParams): StockConnectUrlState {
  const cursor = searchParameters.get("cursor")?.trim();
  const direction = enumOrDefault(searchParameters.get("direction"), stockConnectDirections, "all");
  const requestedChannel = enumOrDefault(
    searchParameters.get("channel"),
    stockConnectChannelSlugs,
    "sh-northbound",
  );
  const directionChannels = stockConnectChannelsForDirection(direction);
  const channel = directionChannels.includes(stockConnectChannelCodeBySlug[requestedChannel])
    ? requestedChannel
    : stockConnectChannelSlugByCode[directionChannels[0] ?? "SH_NORTHBOUND"];

  return {
    date: parseDate(searchParameters.get("date")),
    direction,
    channel,
    ranking: enumOrDefault(searchParameters.get("ranking"), stockConnectRankingSlugs, "active"),
    trendDays: parseTrendDays(searchParameters.get("trendDays")),
    pageSize: parsePageSize(searchParameters.get("pageSize")),
    ...(cursor !== undefined && cursor.length > 0 && cursor.length <= 1024 ? { cursor } : {}),
  };
}

/** 将规范总览或通道详情状态写回可分享 URL。 */
export function serializeStockConnectUrlState(state: StockConnectUrlState): URLSearchParams {
  const searchParameters = new URLSearchParams();

  if (state.date !== "latest") {
    searchParameters.set("date", state.date);
  }
  if (state.direction !== "all") {
    searchParameters.set("direction", state.direction);
  }
  if (state.channel !== "sh-northbound") {
    searchParameters.set("channel", state.channel);
  }
  if (state.ranking !== "active") {
    searchParameters.set("ranking", state.ranking);
  }
  if (state.trendDays !== 20) {
    searchParameters.set("trendDays", String(state.trendDays));
  }
  if (state.pageSize !== 20) {
    searchParameters.set("pageSize", String(state.pageSize));
  }
  if (state.cursor !== undefined) {
    searchParameters.set("cursor", state.cursor);
  }

  return searchParameters;
}

/** 序列化单通道详情筛选；通道由 path 唯一持有，拒绝重复或冲突的方向与通道参数。 */
export function serializeStockConnectChannelDetailUrlState(
  state: StockConnectUrlState,
): URLSearchParams {
  const searchParameters = serializeStockConnectUrlState(state);
  searchParameters.delete("direction");
  searchParameters.delete("channel");
  return searchParameters;
}

/** 从证券上下文 URL 读取并规范化日期、可选通道和历史窗口。 */
export function parseStockConnectSecurityUrlState(
  searchParameters: URLSearchParams,
): StockConnectSecurityUrlState {
  const channelValue = searchParameters.get("channel");

  return {
    date: parseDate(searchParameters.get("date")),
    ...(stockConnectChannelSlugs.includes(channelValue as StockConnectChannelSlug)
      ? { channel: channelValue as StockConnectChannelSlug }
      : {}),
    trendDays: parseTrendDays(searchParameters.get("trendDays")),
  };
}

/** 将规范证券上下文状态写回可分享 URL。 */
export function serializeStockConnectSecurityUrlState(
  state: StockConnectSecurityUrlState,
): URLSearchParams {
  const searchParameters = new URLSearchParams();

  if (state.date !== "latest") {
    searchParameters.set("date", state.date);
  }
  if (state.channel !== undefined) {
    searchParameters.set("channel", state.channel);
  }
  if (state.trendDays !== 20) {
    searchParameters.set("trendDays", String(state.trendDays));
  }

  return searchParameters;
}
