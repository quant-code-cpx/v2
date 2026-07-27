/** 格式化管理时间戳，不展示无决策价值的秒。 */
export const userDateTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

/** 格式化最近成功查询时间，不暗示后端数据新鲜度。 */
export const userListUpdatedTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
