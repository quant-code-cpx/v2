/** 限制 OpenAPI `date` 参数必须是纯日历日期，不能混入时刻。 */
export const DATE_ONLY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

/** 限制知识时刻必须携带秒和明确 UTC 或偏移量，保证跨服务解释唯一。 */
export const OFFSET_DATE_TIME_PATTERN =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i;
