/** 格式化市场价格，固定保留两位小数。 */
export const marketNumberFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 2,
  minimumFractionDigits: 2,
});

/** 紧凑格式化成交额等大数值。 */
export const compactMarketNumberFormatter = new Intl.NumberFormat("zh-CN", {
  notation: "compact",
  maximumFractionDigits: 1,
});

/** 格式化行情更新时间，不暗示秒级实时精度。 */
export function formatMarketUpdatedTime(value: string): string {
  return new Date(value).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}
