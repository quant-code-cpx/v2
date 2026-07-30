import { describe, expect, it } from "vite-plus/test";

import {
  formatStockConnectDecimal,
  formatStockConnectMoneyFact,
  formatStockConnectNetFact,
  stockConnectErrorCopy,
  stockConnectMoneyDirection,
} from "../utils/stock-connect-presentation";

/** 验证金额展示不会把成交额误写成资金流，也不会丢失净额方向。 */
describe("stock connect financial presentation", () => {
  /** 正净额必须同时显示正号和净买入文字。 */
  it("labels positive net amount as net buy", () => {
    expect(
      formatStockConnectNetFact({
        availability: "DERIVED",
        value: { amount: "123456789.01", currency: "HKD", unit: "BASE" },
        lineageRef: "lineage:net",
      }),
    ).toBe("+ HKD 123,456,789.01 · 净买入");
    expect(stockConnectMoneyDirection("123456789.01")).toBe("positive");
  });

  /** 负净额必须同时显示减号和净卖出文字。 */
  it("labels negative net amount as net sell", () => {
    expect(
      formatStockConnectNetFact({
        availability: "REPORTED",
        value: { amount: "-25.50", currency: "CNY", unit: "BASE" },
        lineageRef: "lineage:net",
      }),
    ).toBe("− CNY 25.50 · 净卖出");
    expect(stockConnectMoneyDirection("-25.50")).toBe("negative");
  });

  /** 制度未披露必须显示原因，绝不能渲染零。 */
  it("preserves non-disclosure instead of rendering zero", () => {
    const fact = {
      availability: "NOT_DISCLOSED_BY_REGIME" as const,
      value: null,
      lineageRef: null,
    };

    expect(formatStockConnectNetFact(fact)).toBe("— 未披露（制度）");
    expect(formatStockConnectMoneyFact(fact)).toBe("— 未披露（制度）");
  });

  /** 成交额格式化只做字符串分组，不引入资金流文案或浮点换算。 */
  it("formats turnover decimal strings without flow semantics", () => {
    const formatted = formatStockConnectMoneyFact({
      availability: "REPORTED",
      value: {
        amount: "9007199254740993.123456",
        currency: "CNY",
        unit: "BASE",
      },
      lineageRef: "lineage:turnover",
    });

    expect(formatted).toBe("CNY 9,007,199,254,740,993.123456");
    expect(formatted).not.toContain("净流入");
    expect(formatStockConnectDecimal("000123.00")).toBe("000,123.00");
  });

  /** 父版本失配必须明确声明不会把市场统计与来源榜跨 publication 拼接。 */
  it("explains parent publication mismatch without exposing mixed data", () => {
    expect(stockConnectErrorCopy("PARENT_PUBLICATION_MISMATCH")).toEqual({
      title: "父 publication 已更新",
      description: "市场统计与来源活跃榜必须属于同一父版本；页面不会拼接两个 publication。",
    });
  });
});
