import { z } from "zod";
import { describe, expect, it } from "vite-plus/test";

import {
  marketOverviewQueryOptions,
  marketOverviewRefetchInterval,
  resolveConditionalMarketResponse,
} from "../market";
import {
  marketEquityMoneyFlowRankingsSchema,
  marketIndexBarPageSchema,
  marketSectorEodPageSchema,
  marketSectorEodResourceSchema,
  swIndustryConstituentPageSchema,
} from "../../types/market";

const versionA = "11111111-1111-4111-8111-111111111111";
const versionB = "22222222-2222-4222-8222-222222222222";
const versionC = "33333333-3333-4333-8333-333333333333";
const versionedSchema = z.object({ dataVersion: z.string().uuid() }).strict();
const source = {
  provider: "tushare-pro",
  upstreamSource: "tushare-pro",
  sourceDataset: "moneyflow",
  observedAt: "2026-07-30T18:00:00+08:00",
  adapterVersion: "1",
  schemaFingerprint: "a".repeat(64),
} as const;
const flowItem = {
  exchange: "SSE",
  symbol: "600000",
  name: "浦发银行",
  rank: 1,
  netAmountCny: "100",
  buyLargeAmountCny: null,
  sellLargeAmountCny: null,
  changePercent: null,
} as const;
const swConstituentPage = {
  dataVersion: versionC,
  snapshotDate: "2026-07-30",
  publishedAt: "2026-07-30T18:00:00+08:00",
  historyMode: "latest_revision_effective_interval",
  knowledgeCutoff: "2026-07-30T17:55:00+08:00",
  observedAt: "2026-07-30T17:50:00+08:00",
  source: { ...source, sourceDataset: "index_member_all" },
  inputDataVersions: [versionA, versionB],
  methodology: {
    id: "quant-v2.sw-membership.v1",
    version: "1",
    status: "source_reported",
    temporalSemantics: "latest_revision_effective_interval",
  },
  industry: {
    code: "801010.SI",
    name: "农林牧渔",
    level: 1,
    parentCode: null,
  },
  items: [
    {
      exchange: "SSE",
      symbol: "600000",
      name: "浦发银行",
      inDate: "2026-01-01",
      outDate: "2026-12-31",
      isActive: true,
    },
  ],
  nextCursor: null,
} as const;
const sectorEodMetadata = {
  scheme: "eastmoney.industry",
  tradeDate: "2026-07-30",
  sourceCutoffAt: "2026-07-30T15:30:00+08:00",
  observedAt: "2026-07-30T15:35:00+08:00",
  finality: "post_close_observation",
  qualityStatus: "passed",
  dataVersion: versionC,
  publishedAt: "2026-07-30T18:00:00+08:00",
  inputDataVersions: [versionA, versionB],
} as const;
const sectorEodValue = {
  code: "BK0001",
  name: "示例行业名称",
  latestValue: "1000",
  latestValueUnit: "provider_native",
  changeValue: "10",
  changePercent: "1",
  marketValue: "12345678900",
  marketValueUnit: "CNY",
  turnoverPercent: null,
  advancers: 10,
  decliners: 5,
  leaderName: null,
  leaderChangePercent: null,
} as const;
const indexBarPage = {
  dataVersion: versionA,
  publishedAt: "2026-07-30T18:00:00+08:00",
  index: { indexId: "sse-composite", name: "上证指数" },
  period: "1d",
  source: { ...source, sourceDataset: "index_daily" },
  volumeUnit: "lot",
  inputDataVersions: [versionB],
  items: [
    {
      tradeDate: "2026-07-30",
      open: "3500",
      high: "3520",
      low: "3480",
      close: "3510",
      previousClose: "3490",
      change: "20",
      changePercent: "0.5731",
      volume: null,
      amountCny: null,
      finality: "final",
    },
  ],
  nextCursor: null,
} as const;

// 汇集市场条件读取对响应头与 publication 版本一致性的失败关闭测试。
describe("resolveConditionalMarketResponse", () => {
  // 验证 200 响应头版本与实体版本不一致时拒绝缓存污染。
  it("rejects a 200 response whose X-Data-Version differs from its payload", () => {
    expect(() =>
      resolveConditionalMarketResponse(
        { dataVersion: versionA },
        new Headers({ ETag: '"version-a"', "X-Data-Version": versionB }),
        200,
        undefined,
        versionedSchema,
      ),
    ).toThrowError(expect.objectContaining({ status: 503, code: "data-version-mismatch" }));
  });

  // 验证弱实体标签不能进入行情缓存，避免不同实体共享校验器。
  it("rejects a 200 response with a weak ETag", () => {
    expect(() =>
      resolveConditionalMarketResponse(
        { dataVersion: versionA },
        new Headers({ ETag: 'W/"version-a"', "X-Data-Version": versionA }),
        200,
        undefined,
        versionedSchema,
      ),
    ).toThrowError(expect.objectContaining({ status: 503, code: "invalid-strong-etag" }));
  });

  // 验证没有同 queryKey 已校验实体时不能接受 204。
  it("rejects a 204 response without a previous cache entity", () => {
    expect(() =>
      resolveConditionalMarketResponse(
        undefined,
        new Headers({ ETag: '"version-a"', "X-Data-Version": versionA }),
        204,
        undefined,
        versionedSchema,
      ),
    ).toThrowError(expect.objectContaining({ status: 503, code: "conditional-cache-miss" }));
  });

  // 验证 200 缺少实体时不能伪装成一次成功的 204 条件复用。
  it("rejects a 200 response without an entity body", () => {
    expect(() =>
      resolveConditionalMarketResponse(
        undefined,
        new Headers({ ETag: '"version-a"', "X-Data-Version": versionA }),
        200,
        { payload: { dataVersion: versionA }, etag: '"version-a"' },
        versionedSchema,
      ),
    ).toThrowError(expect.objectContaining({ status: 503, code: "conditional-entity-missing" }));
  });

  // 验证 204 响应版本与缓存实体版本不一致时不能静默复用。
  it("rejects a 204 response whose version differs from the previous entity", () => {
    expect(() =>
      resolveConditionalMarketResponse(
        undefined,
        new Headers({ ETag: '"version-a"', "X-Data-Version": versionB }),
        204,
        { payload: { dataVersion: versionA }, etag: '"version-a"' },
        versionedSchema,
      ),
    ).toThrowError(
      expect.objectContaining({
        status: 503,
        code: "conditional-cache-version-mismatch",
      }),
    );
  });

  // 验证 204 响应的 ETag 与缓存实体不一致时不能复用另一 publication。
  it("rejects a 204 response whose ETag differs from the previous entity", () => {
    expect(() =>
      resolveConditionalMarketResponse(
        undefined,
        new Headers({ ETag: '"version-b"', "X-Data-Version": versionA }),
        204,
        { payload: { dataVersion: versionA }, etag: '"version-a"' },
        versionedSchema,
      ),
    ).toThrowError(
      expect.objectContaining({
        status: 503,
        code: "conditional-cache-etag-mismatch",
      }),
    );
  });

  // 验证 ETag 与 publication 版本均一致的 204 原样复用已校验缓存实体。
  it("accepts a 204 response whose ETag and version match the previous entity", () => {
    const previous = {
      payload: { dataVersion: versionA },
      etag: '"version-a"',
    };

    expect(
      resolveConditionalMarketResponse(
        undefined,
        new Headers({ ETag: '"version-a"', "X-Data-Version": versionA }),
        204,
        previous,
        versionedSchema,
      ),
    ).toBe(previous);
  });

  // 验证版本绑定一致的 200 可返回已校验实体与 ETag。
  it("accepts a 200 response with matching version headers", () => {
    expect(
      resolveConditionalMarketResponse(
        { dataVersion: versionA },
        new Headers({ ETag: '"version-a"', "X-Data-Version": versionA }),
        200,
        undefined,
        versionedSchema,
      ),
    ).toEqual({ payload: { dataVersion: versionA }, etag: '"version-a"' });
  });
});

// 汇集 latest 首页全部会话边界与历史包的刷新策略测试。
describe("marketOverviewRefetchInterval", () => {
  const status = {
    marketState: "trading",
    marketStateAsOf: "2026-07-30T10:00:00+08:00",
    marketStateMethodology: "calendar_schedule_derived",
    eodEligibilityScheduleVersion: "cn-a-eod-eligibility-2026-v1",
    freshness: "current",
    latestEligibleTradeDate: "2026-07-29",
    latestAttemptedTradeDate: "2026-07-29",
    lagTradingDays: 0,
    freshnessReason: "latest_eligible_complete",
    quality: "passed",
  } as const;

  // 验证盘前以一分钟条件刷新捕获开盘边界。
  it("refreshes a pre-open latest session every minute", () => {
    expect(
      marketOverviewRefetchInterval(undefined, {
        ...status,
        marketState: "pre_open",
      }),
    ).toBe(60_000);
  });

  // 验证交易中以一分钟条件刷新捕获会话状态。
  it("refreshes an active latest session every minute", () => {
    expect(marketOverviewRefetchInterval(undefined, status)).toBe(60_000);
  });

  // 验证午间休市仍以一分钟条件刷新捕获午后开市边界。
  it("refreshes a lunch-break latest session every minute", () => {
    expect(
      marketOverviewRefetchInterval(undefined, {
        ...status,
        marketState: "lunch_break",
      }),
    ).toBe(60_000);
  });

  // 验证正常闭市状态降为五分钟条件刷新。
  it("refreshes a current closed latest session every five minutes", () => {
    expect(
      marketOverviewRefetchInterval(undefined, {
        ...status,
        marketState: "closed",
      }),
    ).toBe(5 * 60_000);
  });

  // 验证非交易日状态同样以五分钟条件刷新。
  it("refreshes a current non-trading day every five minutes", () => {
    expect(
      marketOverviewRefetchInterval(undefined, {
        ...status,
        marketState: "non_trading_day",
      }),
    ).toBe(5 * 60_000);
  });

  // 验证陈旧包在上海 17:20 前仍遵循闭市五分钟基线。
  it("uses the closed baseline just before the stale publication window", () => {
    expect(
      marketOverviewRefetchInterval(
        undefined,
        { ...status, marketState: "closed", freshness: "stale" },
        new Date("2026-07-30T09:19:00Z"),
      ),
    ).toBe(5 * 60_000);
  });

  // 验证上海 17:20 起将陈旧包轮询降为十五分钟。
  it("uses fifteen minutes at the stale publication window start", () => {
    expect(
      marketOverviewRefetchInterval(
        undefined,
        { ...status, marketState: "closed", freshness: "stale" },
        new Date("2026-07-30T09:20:00Z"),
      ),
    ).toBe(15 * 60_000);
  });

  // 验证上海 19:59 仍处于陈旧 publication 轮询窗口。
  it("keeps polling a stale publication before 20:00 Shanghai time", () => {
    expect(
      marketOverviewRefetchInterval(
        undefined,
        { ...status, marketState: "closed", freshness: "stale" },
        new Date("2026-07-30T11:59:00Z"),
      ),
    ).toBe(15 * 60_000);
  });

  // 验证上海 20:00 起停止陈旧 publication 自动轮询。
  it("stops polling a stale publication at 20:00 Shanghai time", () => {
    expect(
      marketOverviewRefetchInterval(
        undefined,
        { ...status, marketState: "closed", freshness: "stale" },
        new Date("2026-07-30T12:00:00Z"),
      ),
    ).toBe(false);
  });

  // 验证精确历史包即使当前处于交易会话也保持不可变。
  it("does not refresh an exact historical bundle", () => {
    expect(marketOverviewRefetchInterval("2026-07-29", status)).toBe(false);
  });

  // 验证历史快照不会因标签页重新聚焦而产生隐式网络刷新。
  it("keeps an exact historical query immutable on window focus", () => {
    const options = marketOverviewQueryOptions("2026-07-29");

    expect(options.staleTime).toBe(Number.POSITIVE_INFINITY);
    expect(options.refetchOnWindowFocus).toBe(false);
  });
});

// 汇集资金流榜方向语义的浏览器合同测试。
describe("marketEquityMoneyFlowRankingsSchema", () => {
  // 验证严格正流入和严格负流出能够通过合同。
  it("accepts strictly signed inflow and outflow items", () => {
    expect(
      marketEquityMoneyFlowRankingsSchema.safeParse({
        source,
        methodologyId: "tushare-order-size-flow",
        methodologyVersion: "1",
        universe: "CN-A-SSE-SZSE-TRADED",
        coverage: "1",
        inflow: [flowItem],
        outflow: [{ ...flowItem, netAmountCny: "-100" }],
      }).success,
    ).toBe(true);
  });

  // 验证零值和反向净额不能进入任一方向榜。
  it("rejects zero or reversed directional amounts", () => {
    expect(
      marketEquityMoneyFlowRankingsSchema.safeParse({
        source,
        methodologyId: "tushare-order-size-flow",
        methodologyVersion: "1",
        universe: "CN-A-SSE-SZSE-TRADED",
        coverage: "1",
        inflow: [{ ...flowItem, netAmountCny: "0" }],
        outflow: [{ ...flowItem, netAmountCny: "1" }],
      }).success,
    ).toBe(false);
  });
});

// 汇集指数 K 线成交量单位与 composite 输入版本的浏览器合同测试。
describe("marketIndexBarPageSchema", () => {
  // 验证手为成交量单位且输入 publication 唯一的指数日线页。
  it("accepts lot volume units and unique input publications", () => {
    expect(marketIndexBarPageSchema.safeParse(indexBarPage).success).toBe(true);
  });

  // 验证 composite 输入版本重复时拒绝指数日线页。
  it("rejects duplicate input publication versions", () => {
    expect(
      marketIndexBarPageSchema.safeParse({
        ...indexBarPage,
        inputDataVersions: [versionB, versionB],
      }).success,
    ).toBe(false);
  });
});

// 汇集板块 EOD quote 与 strength composite 发布边界的浏览器合同测试。
describe("marketSectorEod composite schemas", () => {
  // 验证列表与详情都接收两个唯一输入版本组成的 composite。
  it("accepts two unique quote and strength input versions", () => {
    expect(
      marketSectorEodPageSchema.safeParse({
        ...sectorEodMetadata,
        sort: "changePercent",
        order: "desc",
        items: [
          {
            ...sectorEodValue,
            scheme: "eastmoney.industry",
            rank: 1,
            position: 1,
          },
        ],
        nextCursor: null,
      }).success,
    ).toBe(true);
    expect(
      marketSectorEodResourceSchema.safeParse({
        ...sectorEodMetadata,
        ...sectorEodValue,
      }).success,
    ).toBe(true);
  });

  // 验证 quote 与 strength 复用同一输入版本时拒绝 composite。
  it("rejects duplicate component versions", () => {
    expect(
      marketSectorEodResourceSchema.safeParse({
        ...sectorEodMetadata,
        ...sectorEodValue,
        inputDataVersions: [versionA, versionA],
      }).success,
    ).toBe(false);
  });

  // 防止同步层已经换算为人民币元的市值再次被误标为供应商原生单位。
  it("rejects the legacy provider-native market value unit", () => {
    expect(
      marketSectorEodResourceSchema.safeParse({
        ...sectorEodMetadata,
        ...sectorEodValue,
        marketValueUnit: "provider_native",
      }).success,
    ).toBe(false);
  });
});

// 汇集申万成员半开有效区间的浏览器合同测试。
describe("swIndustryConstituentPageSchema", () => {
  // 验证快照日落在纳入和移出日构成的半开区间内。
  it("accepts a constituent interval covering the snapshot date", () => {
    expect(swIndustryConstituentPageSchema.safeParse(swConstituentPage).success).toBe(true);
  });

  // 验证纳入日晚于快照日时拒绝成员记录。
  it("rejects an in-date after the snapshot date", () => {
    expect(
      swIndustryConstituentPageSchema.safeParse({
        ...swConstituentPage,
        items: [{ ...swConstituentPage.items[0], inDate: "2026-07-31" }],
      }).success,
    ).toBe(false);
  });

  // 验证移出日等于快照日时不满足半开区间。
  it("rejects an out-date equal to the snapshot date", () => {
    expect(
      swIndustryConstituentPageSchema.safeParse({
        ...swConstituentPage,
        items: [{ ...swConstituentPage.items[0], outDate: "2026-07-30" }],
      }).success,
    ).toBe(false);
  });

  // 验证双边日期不构成严格非空区间时拒绝成员记录。
  it("rejects an inverted constituent interval", () => {
    expect(
      swIndustryConstituentPageSchema.safeParse({
        ...swConstituentPage,
        items: [
          {
            ...swConstituentPage.items[0],
            inDate: "2026-07-30",
            outDate: "2026-07-29",
          },
        ],
      }).success,
    ).toBe(false);
  });

  // 验证 taxonomy 与成员事实组件不能复用同一输入版本。
  it("rejects duplicate composite input publication versions", () => {
    expect(
      swIndustryConstituentPageSchema.safeParse({
        ...swConstituentPage,
        inputDataVersions: [versionA, versionA],
      }).success,
    ).toBe(false);
  });
});
