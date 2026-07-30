import { describe, expect, it } from "vite-plus/test";

import {
  createDefaultTargetSelector,
  createTargetSelector,
  isTargetSelectorStructurallyReady,
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
        security: { kind: "INSTRUMENT", exchange: "SSE", symbol: "600519" },
      }),
    ).toBe("SECURITY.SSE.600519");
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
  });
});
