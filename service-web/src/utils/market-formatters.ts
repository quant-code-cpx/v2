/** 格式化已通过合同校验的十进制字符串，显示精度不参与业务计算。 */
export function formatMarketDecimal(
  value: string,
  maximumFractionDigits = 2,
  minimumFractionDigits = 0,
): string {
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits,
    minimumFractionDigits,
    useGrouping: true,
  }).format(Number(value));
}

/** 格式化带显式正负号的百分点。 */
export function formatMarketPercent(value: string, digits = 2): string {
  const numeric = Number(value);
  return `${numeric > 0 ? "+" : ""}${numeric.toFixed(digits)}%`;
}

/** 将零到一覆盖率转换为显示百分比，合同仍保留原始比例。 */
export function formatCoverageRatio(value: string): string {
  return `${(Number(value) * 100).toFixed(1)}%`;
}

/** 以亿元展示人民币金额并保留原始正负方向。 */
export function formatCnyYi(value: string): string {
  return `${formatMarketDecimal(String(Number(value) / 100_000_000), 2, 2)} 亿元`;
}

/** 以万亿元展示大盘总成交额。 */
export function formatCnyTrillion(value: string): string {
  return `${formatMarketDecimal(String(Number(value) / 1_000_000_000_000), 2, 2)} 万亿元`;
}

/** 以 Asia/Shanghai 格式化 publication 时间，不暗示秒级实时。 */
export function formatMarketDateTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(value));
}

/** 格式化来源可空字段；空值明确表示来源未报告而不是零。 */
export function formatSourceDecimal(
  value: string | null | undefined,
  suffix = "",
  digits = 2,
): string {
  return value === null || value === undefined
    ? "来源未报告"
    : `${formatMarketDecimal(value, digits, digits)}${suffix}`;
}

/** 将交易所代码转换为页面可读标签，不从 symbol 猜交易所。 */
export function formatExchange(exchange: "SSE" | "SZSE" | "BSE"): string {
  const labels = {
    SSE: "上海证券交易所",
    SZSE: "深圳证券交易所",
    BSE: "北京证券交易所",
  } as const;
  return labels[exchange];
}
