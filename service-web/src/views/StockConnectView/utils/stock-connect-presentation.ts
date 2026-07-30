import type {
  StockConnectAvailability,
  StockConnectChannelCode,
  StockConnectChannelStatus,
  StockConnectMoney,
  StockConnectMoneyFact,
  StockConnectPublication,
  StockConnectRanking,
} from "../../../types/stock-connect";

/** 固定四条通道的产品名称，避免把方向或交易所路径混写。 */
const channelLabels: Record<StockConnectChannelCode, string> = {
  SH_NORTHBOUND: "沪股通",
  SZ_NORTHBOUND: "深股通",
  SH_SOUTHBOUND: "港股通（沪）",
  SZ_SOUTHBOUND: "港股通（深）",
};

/** 固定四条通道的方向与交易场所说明。 */
const channelDescriptions: Record<StockConnectChannelCode, string> = {
  SH_NORTHBOUND: "北向 · SSE",
  SZ_NORTHBOUND: "北向 · SZSE",
  SH_SOUTHBOUND: "南向 · HKEX",
  SZ_SOUTHBOUND: "南向 · HKEX",
};

/** 固定来源代码的人类可读名称。 */
const sourceLabels: Record<StockConnectPublication["sourceRefs"][number]["sourceCode"], string> = {
  HKEX_DATA_MARKETPLACE: "HKEX Data Marketplace",
  HKEX_OMDC: "HKEX OMD-C",
  HKEX_CALENDAR: "HKEX 交易日历",
  SSE_MDGW: "上交所 MDGW",
  SZSE_STEP: "深交所 STEP",
};

/** 返回通道的冻结产品名称。 */
export function stockConnectChannelLabel(channel: StockConnectChannelCode): string {
  return channelLabels[channel];
}

/** 返回通道的业务方向与交易场所说明。 */
export function stockConnectChannelDescription(channel: StockConnectChannelCode): string {
  return channelDescriptions[channel];
}

/** 返回来源活跃榜及榜内净额排序的严格范围名称。 */
export function stockConnectRankingLabel(ranking: StockConnectRanking): string {
  const labels: Record<StockConnectRanking, string> = {
    SOURCE_ACTIVE: "来源活跃证券",
    NET_BUY: "活跃榜内净买入",
    NET_SELL: "活跃榜内净卖出",
  };

  return labels[ranking];
}

/** 将制度与来源状态转换为不会误写零值的中文说明。 */
export function stockConnectAvailabilityLabel(availability: StockConnectAvailability): string {
  const labels: Record<StockConnectAvailability, string> = {
    REPORTED: "已报告",
    DERIVED: "同源字段派生",
    NOT_DISCLOSED_BY_REGIME: "未披露（制度）",
    SOURCE_MISSING: "来源缺失",
    NOT_APPLICABLE: "不适用",
  };

  return labels[availability];
}

/** 对十进制字符串执行纯展示分组，不把金融金额转换成 JavaScript 浮点数。 */
export function formatStockConnectDecimal(amount: string): string {
  const negative = amount.startsWith("-");
  const unsigned = negative ? amount.slice(1) : amount;
  const [whole = "0", fraction] = unsigned.split(".");
  const groupedWhole = whole.replace(/\B(?=(\d{3})+(?!\d))/gu, ",");
  const formatted = fraction === undefined ? groupedWhole : `${groupedWhole}.${fraction}`;

  return negative ? `-${formatted}` : formatted;
}

/** 格式化原币基础单位金额，始终带 CNY/HKD 代码。 */
export function formatStockConnectMoney(value: StockConnectMoney): string {
  return `${value.currency} ${formatStockConnectDecimal(value.amount)}`;
}

/** 返回金额正负方向，不对金额执行加总或推导。 */
export function stockConnectMoneyDirection(amount: string): "positive" | "negative" | "flat" {
  const normalized = amount.replace(/^-|\.|0/gu, "");
  if (normalized.length === 0) {
    return "flat";
  }

  return amount.startsWith("-") ? "negative" : "positive";
}

/** 格式化净额并同时输出符号与净买入、净卖出或持平文字。 */
export function formatStockConnectNetFact(fact: StockConnectMoneyFact): string {
  if (fact.value === null) {
    return `— ${stockConnectAvailabilityLabel(fact.availability)}`;
  }

  const direction = stockConnectMoneyDirection(fact.value.amount);
  const unsignedAmount = fact.value.amount.startsWith("-")
    ? fact.value.amount.slice(1)
    : fact.value.amount;
  const formatted = `${fact.value.currency} ${formatStockConnectDecimal(unsignedAmount)}`;

  if (direction === "positive") {
    return `+ ${formatted} · 净买入`;
  }
  if (direction === "negative") {
    return `− ${formatted} · 净卖出`;
  }

  return `${formatted} · 净额持平`;
}

/** 格式化普通金额事实，不把成交额描述为资金流。 */
export function formatStockConnectMoneyFact(fact: StockConnectMoneyFact): string {
  return fact.value === null
    ? `— ${stockConnectAvailabilityLabel(fact.availability)}`
    : formatStockConnectMoney(fact.value);
}

/** 返回通道会话状态的确定中文说明。 */
export function stockConnectSessionLabel(status: StockConnectChannelStatus): string {
  const labels: Record<StockConnectChannelStatus["sessionState"], string> = {
    OPEN: "交易中",
    CLOSED: "已收盘",
    HALTED: "暂停",
    NOT_OPEN: status.tradingDay ? "尚未开市" : "非交易日",
    UNKNOWN: "状态未知",
  };

  return labels[status.sessionState];
}

/** 返回日终额度状态；阈值以上时绝不把空余额显示为零。 */
export function stockConnectQuotaLabel(status: StockConnectChannelStatus): string {
  if (status.quotaState === "SUFFICIENT") {
    return "额度充足 · 日终";
  }
  if (status.quotaState === "EXHAUSTED") {
    return "额度用尽 · 日终";
  }
  if (status.quotaState === "NOT_APPLICABLE") {
    return "额度不适用";
  }
  if (status.quotaState === "SOURCE_MISSING") {
    return "— 日终额度来源缺失";
  }

  return `${formatStockConnectMoneyFact(status.quotaBalance)} · 日终`;
}

/** 返回买入或卖出委托接受状态，null 保持未知而不是转换为否。 */
export function stockConnectOrderAcceptanceLabel(value: boolean | null): string {
  if (value === null) {
    return "未知";
  }

  return value ? "接受" : "不接受";
}

/** 以中国标准时间显示真实 publication 时间。 */
export function formatStockConnectDateTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

/** 返回 publication 的去重官方来源名称。 */
export function stockConnectSourceSummary(publication: StockConnectPublication): string {
  const sources = publication.sourceRefs.map(
    /** 将稳定来源代码映射为公开产品名称。 */
    (source) => sourceLabels[source.sourceCode],
  );

  return [...new Set(sources)].join("、");
}

/** 将服务端错误 code 映射为不泄露上游细节的恢复提示。 */
export function stockConnectErrorCopy(code?: string): {
  title: string;
  description: string;
} {
  if (code === "EXACT_DATE_NOT_PUBLISHED") {
    return {
      title: "该精确交易日没有 publication",
      description: "可能属于沪深与香港休市差异，或该日尚未完成正式发布。日期不会自动回退。",
    };
  }
  if (code === "PUBLICATION_NOT_READY") {
    return {
      title: "正式 publication 尚未就绪",
      description: "页面不会读取暂存记录或本地样本。保留当前筛选，可稍后原位重试。",
    };
  }
  if (code === "SECURITY_CONTEXT_NOT_FOUND") {
    return {
      title: "未找到证券互联互通上下文",
      description: "该稳定身份在所选日期和通道范围内没有正式来源活跃榜记录。",
    };
  }
  if (code === "CURSOR_VERSION_MISMATCH") {
    return {
      title: "榜单版本已更新",
      description: "旧游标不再属于当前 publication。已保留筛选并回到第一页。",
    };
  }
  if (code === "PARENT_PUBLICATION_MISMATCH") {
    return {
      title: "父 publication 已更新",
      description: "市场统计与来源活跃榜必须属于同一父版本；页面不会拼接两个 publication。",
    };
  }
  if (code === "AUTHORIZATION_FAILED") {
    return {
      title: "无权访问该数据",
      description: "页面未展示任何受限内容，请联系管理员确认服务端授权。",
    };
  }
  if (code === "RATE_LIMITED") {
    return {
      title: "查询频率过高",
      description: "保留当前筛选，等待服务端允许后重试。",
    };
  }

  return {
    title: "互联互通数据暂时不可用",
    description: "保留当前筛选；恢复后可原位重试。页面不会展示未验证或暂存数据。",
  };
}
