export interface MarketMover {
  symbol: string;
  name: string;
  price: number;
  changePercent: number;
  turnover: number;
}

export interface MarketOverview {
  updatedAt: string;
  indexName: string;
  indexValue: number;
  indexChangePercent: number;
  advancing: number;
  declining: number;
  turnover: number;
  movers: readonly MarketMover[];
}
