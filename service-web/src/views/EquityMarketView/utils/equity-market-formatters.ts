import type {
  EquityExchange,
  EquityListingStatus,
  EquityTradingStatus,
} from "../../../types/equity-market";

/** 统一股票中心的小数展示，原始 decimal string 始终保留在 Query 实体中。 */
export function formatDecimal(value: string | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(numeric);
}

/** 将人民币金额按亿、万或元显示，并保留明确单位。 */
export function formatCny(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";

  if (Math.abs(numeric) >= 100_000_000) {
    return `${formatDecimal(String(numeric / 100_000_000), 2)} 亿`;
  }
  if (Math.abs(numeric) >= 10_000) {
    return `${formatDecimal(String(numeric / 10_000), 2)} 万`;
  }
  return `${formatDecimal(value, 2)} 元`;
}

/** 将股数按亿股、万股或股显示。 */
export function formatShares(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";

  if (numeric >= 100_000_000) return `${formatDecimal(String(numeric / 100_000_000), 2)} 亿股`;
  if (numeric >= 10_000) return `${formatDecimal(String(numeric / 10_000), 2)} 万股`;
  return `${formatDecimal(value, 0)} 股`;
}

/** 为中国市场方向值同时返回符号、文本和语义色名，颜色不是唯一信号。 */
export function marketDirection(value: string | null | undefined): {
  label: string;
  color: "error.main" | "success.main" | "text.secondary";
  direction: "up" | "down" | "flat" | "unavailable";
} {
  if (value === null || value === undefined) {
    return { label: "—", color: "text.secondary", direction: "unavailable" };
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return { label: "—", color: "text.secondary", direction: "unavailable" };
  }
  if (numeric > 0) {
    return {
      label: `↑ +${formatDecimal(value, 2)}%`,
      color: "error.main",
      direction: "up",
    };
  }
  if (numeric < 0) {
    return {
      label: `↓ ${formatDecimal(value, 2)}%`,
      color: "success.main",
      direction: "down",
    };
  }
  return { label: "— 0.00%", color: "text.secondary", direction: "flat" };
}

/** 为有正负方向的人民币金额配对箭头、符号与中国市场语义色。 */
export function marketAmountDirection(value: string | null | undefined): {
  label: string;
  color: "error.main" | "success.main" | "text.secondary";
} {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) {
    return { label: "—", color: "text.secondary" };
  }
  const numeric = Number(value);
  if (numeric > 0) return { label: `↑ +${formatCny(value)}`, color: "error.main" };
  if (numeric < 0) return { label: `↓ ${formatCny(value)}`, color: "success.main" };
  return { label: "— 0.00 元", color: "text.secondary" };
}

/** 返回公开交易所的中文显示名称。 */
export function exchangeLabel(exchange: EquityExchange): string {
  if (exchange === "SSE") return "上交所";
  if (exchange === "SZSE") return "深交所";
  return "北交所";
}

/** 返回上市生命周期文案，暂停上市不会写成普通停牌。 */
export function listingStatusLabel(status: EquityListingStatus): string {
  if (status === "LISTED") return "上市";
  if (status === "SUSPENDED") return "暂停上市";
  return "退市";
}

/** 返回目标 EOD 的普通交易状态文案。 */
export function tradingStatusLabel(status: EquityTradingStatus): string {
  if (status === "TRADED") return "正常交易";
  if (status === "TRADE_SUSPENDED") return "停牌";
  if (status === "NO_SESSION") return "非交易日";
  if (status === "NOT_APPLICABLE") return "不适用";
  return "尚未证实";
}

/** 将公开空值原因转换成不会暗示数字零的用户文案。 */
export function nullReasonLabel(reason: string | null | undefined): string {
  if (reason === "NOT_APPLICABLE") return "不适用";
  if (reason === "LEGITIMATE_EMPTY") return "来源确认无值";
  if (reason === "NO_PRIOR_VALUE") return "缺少可比前值";
  if (reason === "NOT_COVERED") return "当前未覆盖";
  return "暂无已发布值";
}
