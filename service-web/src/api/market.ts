import { queryOptions } from "@tanstack/react-query";

import { demoMarketOverview } from "../mocks/market-overview";
import type { MarketOverview } from "../types/market";

async function getMarketOverview(): Promise<MarketOverview> {
  return demoMarketOverview;
}

export const marketOverviewQueryOptions = queryOptions({
  queryKey: ["market", "overview"],
  queryFn: getMarketOverview,
});
