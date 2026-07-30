import type { EtfExchange, EtfListFilters } from "../../../types/etf";

/** ETF 列表固定页大小，避免 URL 任意扩大 typed reader 负载。 */
export const etfListPageSize = 50;

/** ETF 上市场所白名单。 */
const exchanges = new Set<EtfExchange>(["SSE", "SZSE"]);

/** 读取长度受限的可选 URL 文本，空白与超长值统一丢弃。 */
function optionalText(value: string | null, maximum: number): string | undefined {
  const normalized = value?.trim();

  return normalized !== undefined && normalized.length > 0 && normalized.length <= maximum
    ? normalized
    : undefined;
}

/** 把不受信任查询字符串收敛为 ETF 列表白名单状态。 */
export function readEtfListUrlState(search: URLSearchParams): EtfListFilters {
  const exchangeValue = search.get("exchange");
  const sortValue = search.get("sort");
  const orderValue = search.get("order");
  const pageValue = Number.parseInt(search.get("page") ?? "1", 10);
  const cursor = optionalText(search.get("cursor"), 2_048);

  return {
    exchange:
      exchangeValue !== null && exchanges.has(exchangeValue as EtfExchange)
        ? (exchangeValue as EtfExchange)
        : "SSE",
    q: optionalText(search.get("q"), 40),
    sort: sortValue === "displayName" ? "displayName" : "symbol",
    order: orderValue === "desc" ? "desc" : "asc",
    cursor,
    page:
      cursor !== undefined &&
      Number.isSafeInteger(pageValue) &&
      pageValue > 0 &&
      pageValue <= 10_000
        ? pageValue
        : 1,
    pageSize: etfListPageSize,
  };
}

/** 将 ETF 列表状态写回稳定 URL，并省略默认值与空过滤器。 */
export function writeEtfListUrlState(filters: EtfListFilters): URLSearchParams {
  const search = new URLSearchParams();

  if (filters.exchange !== "SSE") {
    search.set("exchange", filters.exchange);
  }
  if (filters.q !== undefined) {
    search.set("q", filters.q);
  }
  if (filters.sort !== "symbol") {
    search.set("sort", filters.sort);
  }
  if (filters.order !== "asc") {
    search.set("order", filters.order);
  }
  if (filters.cursor !== undefined) {
    search.set("cursor", filters.cursor);
  }
  if (filters.page !== 1) {
    search.set("page", String(filters.page));
  }

  return search;
}

/** 更换筛选或排序后回到对应 publication 查询的第一页。 */
export function resetEtfListCursor(
  filters: EtfListFilters,
  changes: Partial<Pick<EtfListFilters, "exchange" | "q" | "sort" | "order">>,
): EtfListFilters {
  return {
    ...filters,
    ...changes,
    cursor: undefined,
    page: 1,
  };
}
