import { describe, expect, it } from "vite-plus/test";

import {
  createDefaultTargetSelector,
  createTargetSelector,
  indexTargetForDataset,
  isTargetSelectorStructurallyReady,
  marginTargetForDataset,
  moneyFlowTargetForDataset,
  stockConnectResearchTargetForDataset,
  targetSelectorSummary,
} from "../utils/target-selector";

/** 验证数据运维目标选择器的默认值、完整性与安全摘要。 */
describe("data operations target selector", () => {
  /** capability 允许时，完整数据集操作只能以受限 `GLOBAL` selector 为默认值。 */
  it("prefers GLOBAL only when the dataset capability allows it", () => {
    expect(createDefaultTargetSelector(["INSTRUMENT", "GLOBAL"], "equity.daily")).toEqual({
      kind: "GLOBAL",
    });
    expect(createDefaultTargetSelector(["INSTRUMENT"], "equity.daily")).toEqual({
      kind: "INSTRUMENT",
      exchange: "SSE",
      symbol: "",
    });
  });

  /** 四个 ETF canonical dataset 只生成各自固定操作，未知 ETF 数据集不创建草稿。 */
  it("binds ETF dataset codes to one operation", () => {
    expect(createDefaultTargetSelector(["ETF"], "fund.etf.profile.reported")).toEqual({
      kind: "ETF",
      operation: "MASTER",
      venue: null,
      scope: "ALL_VENUES",
      etf: null,
    });
    expect(createDefaultTargetSelector(["ETF"], "fund.etf.trading_state.reported")).toMatchObject({
      operation: "STATUS",
    });
    expect(createDefaultTargetSelector(["ETF"], "fund.etf.bar.1d.reported")).toMatchObject({
      operation: "BARS",
      venue: null,
    });
    expect(createDefaultTargetSelector(["ETF"], "fund.etf.nav.1d.reported")).toMatchObject({
      operation: "NAV",
    });
    expect(createDefaultTargetSelector(["ETF"], "fund.etf.unknown")).toBeUndefined();
  });

  /** 六个指数数据集只生成与服务端一致的管理方、能力及目录或单指数代码草稿。 */
  it("binds index dataset codes to controlled selector shapes", () => {
    expect(createDefaultTargetSelector(["INDEX"], "index.csi.catalog.snapshot")).toEqual({
      kind: "INDEX",
      administrator: "CSI",
      capability: "index.catalog.snapshot",
      indexCode: null,
    });
    expect(createDefaultTargetSelector(["INDEX"], "index.csi.constituent.snapshot")).toEqual({
      kind: "INDEX",
      administrator: "CSI",
      capability: "index.constituent.snapshot",
      indexCode: "",
    });
    expect(createDefaultTargetSelector(["INDEX"], "index.csi.weight.snapshot")).toMatchObject({
      administrator: "CSI",
      capability: "index.weight.snapshot",
      indexCode: "",
    });
    expect(createDefaultTargetSelector(["INDEX"], "index.cni.catalog.snapshot")).toEqual({
      kind: "INDEX",
      administrator: "CNI",
      capability: "index.catalog.snapshot",
      indexCode: null,
    });
    expect(createDefaultTargetSelector(["INDEX"], "index.cni.constituent.snapshot")).toMatchObject({
      administrator: "CNI",
      capability: "index.constituent.snapshot",
      indexCode: "",
    });
    expect(createDefaultTargetSelector(["INDEX"], "index.cni.weight.snapshot")).toMatchObject({
      administrator: "CNI",
      capability: "index.weight.snapshot",
      indexCode: "",
    });
    expect(indexTargetForDataset("index.unknown.snapshot")).toBeUndefined();
  });

  /** 两个资金流数据集只生成各自固定操作的有效最小草稿，未知数据集 fail-closed。 */
  it("binds money flow dataset codes to daily or ranking drafts", () => {
    expect(createDefaultTargetSelector(["MONEY_FLOW"], "money_flow.daily")).toEqual({
      kind: "MONEY_FLOW",
      operation: "DAILY",
      scope: "MARKET",
    });
    expect(createDefaultTargetSelector(["MONEY_FLOW"], "money_flow.ranking")).toEqual({
      kind: "MONEY_FLOW",
      operation: "RANKING",
      methodology: "EASTMONEY_ORDER_SIZE",
      scope: "EQUITY",
      window: "TODAY",
    });
    expect(moneyFlowTargetForDataset("money_flow.unknown")).toBeUndefined();
  });

  /** 三类两融数据集必须固定 operation；资格快照默认选择已有真实来源的北交所范围。 */
  it("binds margin dataset codes to strict market-level defaults", () => {
    expect(createDefaultTargetSelector(["MARGIN"], "market.margin.market.1d.reported")).toEqual({
      kind: "MARGIN",
      operation: "MARKET",
      venue: "SSE",
      security: null,
    });
    expect(createDefaultTargetSelector(["MARGIN"], "market.margin.security.1d.reported")).toEqual({
      kind: "MARGIN",
      operation: "SECURITY",
      venue: "SSE",
      security: null,
    });
    expect(createDefaultTargetSelector(["MARGIN"], "market.margin.eligibility.reported")).toEqual({
      kind: "MARGIN",
      operation: "ELIGIBILITY",
      venue: "BSE",
      security: null,
    });
    expect(marginTargetForDataset("market.margin.unknown")).toBeUndefined();
  });

  /** 非全局 selector 在缺少合同必填字段时不能进入预检。 */
  it("requires the strict union fields before preflight", () => {
    const instrument = createTargetSelector("INSTRUMENT", "equity.daily");
    const sector = createTargetSelector("SECTOR", "sector.catalog");
    const global = createTargetSelector("GLOBAL", "market.overview");

    expect(isTargetSelectorStructurallyReady(global!)).toBe(true);
    expect(isTargetSelectorStructurallyReady(instrument!)).toBe(false);
    expect(isTargetSelectorStructurallyReady(sector!)).toBe(false);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "INSTRUMENT",
        exchange: "SSE",
        symbol: "600519",
      }),
    ).toBe(true);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "ETF",
        operation: "BARS",
        venue: "SSE",
        etf: "SSE.510300",
      }),
    ).toBe(true);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "MARGIN",
        operation: "ELIGIBILITY",
        venue: "BSE",
        security: null,
      }),
    ).toBe(true);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "MARGIN",
        operation: "ELIGIBILITY",
        venue: "SSE",
        security: null,
      } as never),
    ).toBe(false);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "MARGIN",
        operation: "SECURITY",
        venue: "SZSE",
        security: { kind: "INSTRUMENT", exchange: "SZSE", symbol: "000001" },
      } as never),
    ).toBe(false);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "INDEX",
        administrator: "CSI",
        capability: "index.catalog.snapshot",
        indexCode: null,
      }),
    ).toBe(true);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "INDEX",
        administrator: "CNI",
        capability: "index.constituent.snapshot",
        indexCode: "ABC12345",
      }),
    ).toBe(true);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "INDEX",
        administrator: "CNI",
        capability: "index.weight.snapshot",
        indexCode: "ABC12",
      }),
    ).toBe(false);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "MONEY_FLOW",
        operation: "DAILY",
        scope: "EQUITY",
        exchange: "SSE",
        symbol: "600000",
      }),
    ).toBe(true);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "MONEY_FLOW",
        operation: "DAILY",
        scope: "EQUITY",
        exchange: "SSE",
        symbol: "60000",
      }),
    ).toBe(false);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "MONEY_FLOW",
        operation: "DAILY",
        scope: "SECTOR",
        scheme: "eastmoney.industry",
        sectorCode: "BK0475",
      }),
    ).toBe(true);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "MONEY_FLOW",
        operation: "DAILY",
        scope: "MARKET",
      }),
    ).toBe(true);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "MONEY_FLOW",
        operation: "RANKING",
        methodology: "EASTMONEY_ORDER_SIZE",
        scope: "EQUITY",
        window: "DAY_3",
      }),
    ).toBe(true);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "MONEY_FLOW",
        operation: "RANKING",
        methodology: "EASTMONEY_ORDER_SIZE",
        scope: "SECTOR",
        sectorType: "REGION",
        window: "DAY_5",
      }),
    ).toBe(true);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "MONEY_FLOW",
        operation: "RANKING",
        methodology: "THS_TRADE_DIRECTION",
        scope: "INDUSTRY",
        window: "DAY_20",
      }),
    ).toBe(true);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "INDEX",
        administrator: "CNI",
        capability: "index.weight.snapshot",
        indexCode: "ABC123456",
      }),
    ).toBe(false);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "INDEX",
        administrator: "CNI",
        capability: "index.weight.snapshot",
        indexCode: "abc123",
      }),
    ).toBe(false);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "ETF",
        operation: "BARS",
        venue: "SZSE",
        etf: "SSE.510300",
      }),
    ).toBe(false);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "ETF",
        operation: "BARS",
        venue: null,
        etf: "SSE.510300",
      }),
    ).toBe(true);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "ETF",
        operation: "MASTER",
        venue: null,
        scope: "ALL_VENUES",
        etf: null,
      }),
    ).toBe(true);
    expect(
      isTargetSelectorStructurallyReady({
        kind: "ETF",
        operation: "NAV",
        venue: null,
        scope: "ALL_ETFS",
        etf: null,
        profileDataVersions: null,
      }),
    ).toBe(true);
  });

  /** 沪深港通默认必须覆盖四通道完整数据包，避免重复 datasetCode 才能拼齐范围。 */
  it("defaults stock connect to the complete four-channel bundle", () => {
    const selector = createTargetSelector("STOCK_CONNECT", "market.stock_connect.overview.bundle");

    expect(selector).toEqual({
      kind: "STOCK_CONNECT",
      operation: "MARKET",
      channel: "ALL",
      direction: null,
    });
    expect(isTargetSelectorStructurallyReady(selector!)).toBe(true);
    expect(targetSelectorSummary(selector!)).toBe("完整互联互通数据包 · 全部通道 · 全部方向");
  });

  /** 港通市场统计 `research` 只为唯一数据集生成默认范围，且不复用正式 `bundle` `selector`。 */
  it("defaults stock-connect market-stat research independently and fail-closed", () => {
    const selector = createTargetSelector(
      "STOCK_CONNECT_RESEARCH",
      "market.stock_connect.market_stat.research",
    );

    expect(selector).toEqual({
      kind: "STOCK_CONNECT_RESEARCH",
      operation: "MARKET_STAT",
      channel: "ALL",
      direction: null,
    });
    expect(
      stockConnectResearchTargetForDataset("market.stock_connect.unknown.research"),
    ).toBeUndefined();
    expect(
      createTargetSelector("STOCK_CONNECT_RESEARCH", "market.stock_connect.unknown.research"),
    ).toBeUndefined();
    expect(isTargetSelectorStructurallyReady(selector!)).toBe(true);
    expect(targetSelectorSummary(selector!)).toBe("港通市场统计（research） · 全部通道 · 全部方向");
  });

  /** selector 摘要只表达公开业务范围，不混入 Provider 参数或内部执行状态。 */
  it("builds a safe selector summary", () => {
    expect(
      targetSelectorSummary({
        kind: "ETF",
        operation: "MASTER",
        venue: null,
        scope: "ALL_VENUES",
        etf: null,
      }),
    ).toBe("MASTER.沪深全市场");
    expect(
      targetSelectorSummary({
        kind: "MARGIN",
        operation: "SECURITY",
        venue: "SSE",
        security: null,
      }),
    ).toBe("SECURITY.SSE");
    expect(
      targetSelectorSummary({
        kind: "ETF",
        operation: "STATUS",
        venue: null,
        scope: "ALL_ETFS",
        etf: null,
        profileDataVersions: {
          SSE: "00000000-0000-4000-8000-000000000011",
          SZSE: "00000000-0000-4000-8000-000000000012",
        },
      }),
    ).toBe("STATUS.全部已发布 ETF");
    expect(
      targetSelectorSummary({
        kind: "INDEX",
        administrator: "CSI",
        capability: "index.catalog.snapshot",
        indexCode: null,
      }),
    ).toBe("CSI.index.catalog.snapshot");
    expect(
      targetSelectorSummary({
        kind: "INDEX",
        administrator: "CNI",
        capability: "index.weight.snapshot",
        indexCode: "ABC12345",
      }),
    ).toBe("CNI.index.weight.snapshot.ABC12345");
    expect(
      targetSelectorSummary({
        kind: "MONEY_FLOW",
        operation: "DAILY",
        scope: "EQUITY",
        exchange: "SSE",
        symbol: "600000",
      }),
    ).toBe("DAILY.EQUITY.SSE.600000");
    expect(
      targetSelectorSummary({
        kind: "MONEY_FLOW",
        operation: "RANKING",
        methodology: "EASTMONEY_ORDER_SIZE",
        scope: "SECTOR",
        sectorType: "CONCEPT",
        window: "DAY_10",
      }),
    ).toBe("RANKING.EASTMONEY_ORDER_SIZE.SECTOR.CONCEPT.DAY_10");
  });
});
