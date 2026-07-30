import type {
  EquityExchange,
  EquityListingStatus,
  EquitySearchSortField,
  EquityTradingStatus,
} from "../../../types/equity-market";

/** 描述股票中心列表可分享、可刷新的完整 URL 状态。 */
export interface EquityMarketUrlState {
  q?: string;
  exchanges: EquityExchange[];
  listingStatuses: EquityListingStatus[];
  tradingStatuses: EquityTradingStatus[];
  industries: string[];
  concepts: string[];
  swIndustries: string[];
  sort: EquitySearchSortField;
  order: "asc" | "desc";
  page: number;
  limit: number;
  cursor?: string;
  dataVersion?: string;
}

/** 股票中心允许出现在 URL 中的交易所集合。 */
const exchanges = new Set<EquityExchange>(["SSE", "SZSE", "BSE"]);

/** 股票中心当前可查询的上市生命周期集合；暂停上市在来源覆盖前失败关闭。 */
const listingStatuses = new Set<EquityListingStatus>(["LISTED", "DELISTED"]);

/** 股票中心允许出现在 URL 中的普通交易状态集合。 */
const tradingStatuses = new Set<EquityTradingStatus>([
  "TRADED",
  "TRADE_SUSPENDED",
  "NO_SESSION",
  "NOT_APPLICABLE",
  "UNKNOWN",
]);

/** 股票中心公开排序白名单，与 discovery 服务端索引字段一一对应。 */
const sortFields = new Set<EquitySearchSortField>([
  "symbol",
  "name",
  "close",
  "changePercent",
  "amountCny",
  "turnoverRate",
  "totalMarketCap",
  "floatMarketCap",
  "peTtm",
  "pb",
]);

/** 从重复 URL 参数读取去重后的受控枚举数组。 */
function readEnumValues<T extends string>(
  search: URLSearchParams,
  key: string,
  values: Set<T>,
): T[] {
  return Array.from(
    new Set(search.getAll(key).filter((value): value is T => values.has(value as T))),
  );
}

/** 从重复 URL 参数读取最多二十个非空分类代码。 */
function readCodes(search: URLSearchParams, key: string): string[] {
  return Array.from(
    new Set(
      search
        .getAll(key)
        .map((value) => value.trim())
        .filter((value) => value.length > 0 && value.length <= 64),
    ),
  ).slice(0, 20);
}

/** 读取有界正整数，非法值回到明确默认值。 */
function readPositiveInteger(value: string | null, fallback: number, maximum: number): number {
  if (value === null || !/^[1-9]\d*$/.test(value)) {
    return fallback;
  }

  return Math.min(Number(value), maximum);
}

/** 解析并规范化股票中心 URL；未知值不会进入 API 请求。 */
export function readEquityMarketUrl(search: URLSearchParams): EquityMarketUrlState {
  const qValue = search.get("q")?.trim();
  const sortValue = search.get("sort");
  const orderValue = search.get("order");
  const cursorValue = search.get("cursor");
  const dataVersionValue = search.get("dataVersion");

  const normalizedListingStatuses = readEnumValues(search, "status", listingStatuses);

  return {
    ...(qValue !== undefined && qValue.length > 0 && qValue.length <= 64 ? { q: qValue } : {}),
    exchanges: readEnumValues(search, "exchange", exchanges),
    listingStatuses:
      normalizedListingStatuses.length === 0 ? ["LISTED"] : normalizedListingStatuses,
    tradingStatuses: readEnumValues(search, "tradingStatus", tradingStatuses),
    industries: readCodes(search, "industry"),
    concepts: readCodes(search, "concept"),
    swIndustries: readCodes(search, "sw"),
    sort: sortFields.has(sortValue as EquitySearchSortField)
      ? (sortValue as EquitySearchSortField)
      : "symbol",
    order: orderValue === "desc" ? "desc" : "asc",
    page: readPositiveInteger(search.get("page"), 1, 10_000),
    limit: readPositiveInteger(search.get("limit"), 50, 100),
    ...(cursorValue !== null && cursorValue.length <= 1024 ? { cursor: cursorValue } : {}),
    ...(dataVersionValue !== null && /^[0-9a-f-]{36}$/i.test(dataVersionValue)
      ? { dataVersion: dataVersionValue }
      : {}),
  };
}

/** 将一个多选参数按稳定顺序写回 URL。 */
function writeValues(search: URLSearchParams, key: string, values: readonly string[]): void {
  search.delete(key);
  for (const value of values) {
    search.append(key, value);
  }
}

/** 把规范化列表状态编码成稳定、可分享的查询字符串。 */
export function writeEquityMarketUrl(state: EquityMarketUrlState): URLSearchParams {
  const search = new URLSearchParams();

  if (state.q !== undefined) search.set("q", state.q);
  writeValues(search, "exchange", state.exchanges);
  writeValues(search, "status", state.listingStatuses);
  writeValues(search, "tradingStatus", state.tradingStatuses);
  writeValues(search, "industry", state.industries);
  writeValues(search, "concept", state.concepts);
  writeValues(search, "sw", state.swIndustries);
  if (state.sort !== "symbol") search.set("sort", state.sort);
  if (state.order !== "asc") search.set("order", state.order);
  if (state.page > 1) search.set("page", String(state.page));
  if (state.limit !== 50) search.set("limit", String(state.limit));
  if (state.cursor !== undefined) search.set("cursor", state.cursor);
  if (state.dataVersion !== undefined) search.set("dataVersion", state.dataVersion);

  return search;
}

/** 描述股票详情可分享的页签与行情控制状态。 */
export interface EquityDetailUrlState {
  tab:
    | "market"
    | "company"
    | "financial"
    | "valuation"
    | "money-flow"
    | "sectors"
    | "events"
    | "data-status";
  period: "1d" | "1w" | "1mo";
  adjust: "none" | "qfq" | "hfq";
  range: "1y" | "3y" | "all";
  identityAsOf?: string;
}

/** 详情页签白名单。 */
const detailTabs = new Set<EquityDetailUrlState["tab"]>([
  "market",
  "company",
  "financial",
  "valuation",
  "money-flow",
  "sectors",
  "events",
  "data-status",
]);

/** 读取真实 ISO 公历日，防止无效点时锚进入证券身份查询。 */
function readDateOnly(value: string | null): string | undefined {
  const matched = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value ?? "");
  if (matched === null || value === null) return undefined;
  const parsed = new Date(`${value}T00:00:00Z`);
  return parsed.getUTCFullYear() === Number(matched[1]) &&
    parsed.getUTCMonth() + 1 === Number(matched[2]) &&
    parsed.getUTCDate() === Number(matched[3])
    ? value
    : undefined;
}

/** 解析详情 URL；图表十字线、缩放等高频状态不会进入 React。 */
export function readEquityDetailUrl(search: URLSearchParams): EquityDetailUrlState {
  const tab = search.get("tab");
  const period = search.get("period");
  const adjust = search.get("adjust");
  const range = search.get("range");
  const identityAsOf = readDateOnly(search.get("asOf"));

  return {
    tab: detailTabs.has(tab as EquityDetailUrlState["tab"])
      ? (tab as EquityDetailUrlState["tab"])
      : "market",
    period: period === "1w" || period === "1mo" ? period : "1d",
    adjust: adjust === "qfq" || adjust === "hfq" ? adjust : "none",
    range: range === "1y" || range === "all" ? range : "3y",
    ...(identityAsOf === undefined ? {} : { identityAsOf }),
  };
}

/** 把详情页显式业务控制写回 URL，保留可复制的分析上下文。 */
export function writeEquityDetailUrl(state: EquityDetailUrlState): URLSearchParams {
  const search = new URLSearchParams();

  if (state.tab !== "market") search.set("tab", state.tab);
  if (state.period !== "1d") search.set("period", state.period);
  if (state.adjust !== "none") search.set("adjust", state.adjust);
  if (state.range !== "3y") search.set("range", state.range);
  if (state.identityAsOf !== undefined) search.set("asOf", state.identityAsOf);

  return search;
}
