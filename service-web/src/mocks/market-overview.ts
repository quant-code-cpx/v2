import type { MarketOverview } from "../types/market";

export const demoMarketOverview: MarketOverview = {
  updatedAt: "2026-07-26T07:00:00.000Z",
  indexName: "沪深300",
  indexValue: 4_126.78,
  indexChangePercent: 0.82,
  advancing: 3_106,
  declining: 1_684,
  turnover: 986_400_000_000,
  movers: [
    {
      symbol: "600519",
      name: "贵州茅台",
      price: 1_468.22,
      changePercent: 1.74,
      turnover: 4_280_000_000,
    },
    {
      symbol: "300750",
      name: "宁德时代",
      price: 204.61,
      changePercent: 2.18,
      turnover: 3_890_000_000,
    },
    {
      symbol: "601318",
      name: "中国平安",
      price: 62.48,
      changePercent: -0.54,
      turnover: 2_160_000_000,
    },
  ],
};
