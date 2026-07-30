import type {
  StockConnectChannelCode,
  StockConnectInstrumentIdentity,
} from "../../../types/stock-connect";
import { stockConnectChannelSlugByCode } from "./stock-connect-url";

/** 描述来源活跃证券名称主链接与可选互联互通记录次链接。 */
export interface StockConnectSecurityLinks {
  primaryPath: string | null;
  contextPath: string | null;
}

/** 按稳定身份能力选择详情路径，未解析身份绝不生成可能指向错误证券的链接。 */
export function resolveStockConnectSecurityLinks(
  identity: StockConnectInstrumentIdentity,
  channel: StockConnectChannelCode,
  resolvedTradeDate: string,
): StockConnectSecurityLinks {
  if (identity.identityAvailability === "SOURCE_UNRESOLVED") {
    return { primaryPath: null, contextPath: null };
  }

  const contextSearch = new URLSearchParams({
    date: resolvedTradeDate,
    channel: stockConnectChannelSlugByCode[channel],
  }).toString();
  const contextPath = `/market/stock-connect/securities/${encodeURIComponent(
    identity.instrumentEntityRef,
  )}?${contextSearch}`;
  const isCanonicalAshare =
    (identity.listingVenue === "SSE" || identity.listingVenue === "SZSE") &&
    /^\d{6}$/.test(identity.sourceSecurityCode);

  if (isCanonicalAshare) {
    return {
      primaryPath: `/market/equities/${identity.listingVenue}/${identity.sourceSecurityCode}`,
      contextPath,
    };
  }

  // 港股详情尚不存在；其他不满足 A 股路径合同的已解析身份也安全降级到互联互通上下文。
  return { primaryPath: contextPath, contextPath: null };
}
