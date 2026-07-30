import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Table, TableBody } from "@mui/material";
import { describe, expect, it } from "vite-plus/test";

import type {
  StockConnectActiveSecurity,
  StockConnectInstrumentIdentity,
} from "../../../types/stock-connect";
import { ActiveSecurityTableRow } from "../components/ActiveSecurityTableRow";
import { resolveStockConnectSecurityLinks } from "../utils/stock-connect-security-links";

/** 返回字段不可用且绝不以零补值的金额事实。 */
function unavailableMoneyFact(): StockConnectActiveSecurity["buyAmount"] {
  return {
    availability: "SOURCE_MISSING",
    value: null,
    lineageRef: null,
  };
}

/** 构造只用于链接渲染回归的最小来源活跃证券记录。 */
function activeSecurity(identity: StockConnectInstrumentIdentity): StockConnectActiveSecurity {
  return {
    rankingRank: 1,
    sourceRank: 1,
    identity,
    buyAmount: unavailableMoneyFact(),
    sellAmount: unavailableMoneyFact(),
    turnoverAmount: unavailableMoneyFact(),
    netBuyAmount: unavailableMoneyFact(),
  };
}

/** 构造已解析的沪市 A 股稳定身份。 */
function resolvedSseIdentity(): StockConnectInstrumentIdentity {
  return {
    identityAvailability: "RESOLVED",
    instrumentEntityRef: "equity/SSE:600000?revision=1",
    sourceSecurityCode: "600000",
    displayName: "沪市证券",
    listingVenue: "SSE",
  };
}

/** 构造当前没有完整详情页的已解析港股稳定身份。 */
function resolvedHkexIdentity(): StockConnectInstrumentIdentity {
  return {
    identityAvailability: "RESOLVED",
    instrumentEntityRef: "equity/HKEX:00700?revision=2",
    sourceSecurityCode: "00700",
    displayName: "港股证券",
    listingVenue: "HKEX",
  };
}

/** 构造不允许生成任何证券链接的历史未解析身份。 */
function unresolvedIdentity(): StockConnectInstrumentIdentity {
  return {
    identityAvailability: "SOURCE_UNRESOLVED",
    instrumentEntityRef: null,
    sourceSecurityCode: "00999",
    displayName: "历史未解析证券",
    listingVenue: "HKEX",
  };
}

/** 验证纯函数严格区分 A 股详情、港股降级与历史未解析身份。 */
describe("resolveStockConnectSecurityLinks", () => {
  /** A 股名称进入既有详情，context 次链接编码稳定实体引用。 */
  it("returns canonical A-share and encoded context paths", () => {
    expect(
      resolveStockConnectSecurityLinks(resolvedSseIdentity(), "SH_NORTHBOUND", "2026-07-30"),
    ).toEqual({
      primaryPath: "/market/equities/SSE/600000",
      contextPath:
        "/market/stock-connect/securities/equity%2FSSE%3A600000%3Frevision%3D1?date=2026-07-30&channel=sh-northbound",
    });
  });

  /** 港股详情能力缺失时，证券名称只降级到已存在的互联互通上下文。 */
  it("uses the context as the only HKEX destination", () => {
    expect(
      resolveStockConnectSecurityLinks(resolvedHkexIdentity(), "SH_SOUTHBOUND", "2026-07-30"),
    ).toEqual({
      primaryPath:
        "/market/stock-connect/securities/equity%2FHKEX%3A00700%3Frevision%3D2?date=2026-07-30&channel=sh-southbound",
      contextPath: null,
    });
  });

  /** 来源身份未解析时不根据来源代码猜测任何站内详情路径。 */
  it("returns no destination for an unresolved historical identity", () => {
    expect(
      resolveStockConnectSecurityLinks(unresolvedIdentity(), "SZ_SOUTHBOUND", "2026-07-30"),
    ).toEqual({
      primaryPath: null,
      contextPath: null,
    });
  });
});

/** 验证表格行把纯路径决策转化为可聚焦且不重复的链接。 */
describe("ActiveSecurityTableRow links", () => {
  /** 三类身份同时渲染时，A 股有主次入口、港股仅降级主入口、未解析身份无链接。 */
  it("renders accessible links according to identity capability", () => {
    render(
      <MemoryRouter>
        <Table>
          <TableBody>
            <ActiveSecurityTableRow
              item={activeSecurity(resolvedSseIdentity())}
              channel="SH_NORTHBOUND"
              resolvedTradeDate="2026-07-30"
            />
            <ActiveSecurityTableRow
              item={activeSecurity(resolvedHkexIdentity())}
              channel="SH_SOUTHBOUND"
              resolvedTradeDate="2026-07-30"
            />
            <ActiveSecurityTableRow
              item={activeSecurity(unresolvedIdentity())}
              channel="SZ_SOUTHBOUND"
              resolvedTradeDate="2026-07-30"
            />
          </TableBody>
        </Table>
      </MemoryRouter>,
    );

    const rows = screen.getAllByRole("row");
    const [aShareElement, hkexElement, unresolvedElement] = rows;
    if (
      aShareElement === undefined ||
      hkexElement === undefined ||
      unresolvedElement === undefined
    ) {
      throw new Error("来源活跃证券测试行数量不足。");
    }
    const aShareRow = within(aShareElement);
    const hkexRow = within(hkexElement);
    const unresolvedRow = within(unresolvedElement);

    expect(aShareRow.getByRole("link", { name: "沪市证券" })).toHaveAttribute(
      "href",
      "/market/equities/SSE/600000",
    );
    expect(aShareRow.getByRole("link", { name: "互联互通记录" })).toHaveAttribute(
      "href",
      "/market/stock-connect/securities/equity%2FSSE%3A600000%3Frevision%3D1?date=2026-07-30&channel=sh-northbound",
    );
    expect(hkexRow.getByRole("link", { name: "港股证券" })).toHaveAttribute(
      "href",
      "/market/stock-connect/securities/equity%2FHKEX%3A00700%3Frevision%3D2?date=2026-07-30&channel=sh-southbound",
    );
    expect(hkexRow.queryByRole("link", { name: "互联互通记录" })).not.toBeInTheDocument();
    expect(unresolvedRow.queryAllByRole("link")).toHaveLength(0);
  });
});
