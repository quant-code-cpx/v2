import { z } from 'zod';

import { PublicProblemException } from '../../../common/exceptions/problem.exception.js';
import {
  EQUITY_DATASET_FAMILIES,
  EQUITY_DISCOVERY_COLUMNS,
  EQUITY_DISCOVERY_SORT_FIELDS,
  EQUITY_EVENT_FAMILIES,
  EQUITY_MEMBERSHIP_SCHEMES,
  EQUITY_MONEY_FLOW_BUCKETS,
  EQUITY_TRADING_STATUSES,
  EQUITY_VALUATION_METRICS,
} from '../../../data-sync/contracts/equity-workspace.contract.js';
import {
  EQUITY_EXCHANGES,
  EQUITY_LISTING_STATUSES,
} from '../../../data-sync/contracts/equity-instrument.contract.js';

const dateSchema = z.string().date();
const dateTimeSchema = z.string().datetime({ offset: true });
const decimalSchema = z.string().regex(/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/);

/** 去除搜索文本前后空白，同时保留非字符串供 Zod 拒绝。 */
function trimOptionalString(value: unknown): unknown {
  return typeof value === 'string' ? value.trim() : value;
}

/** 校验数值筛选范围，至少必须给出一个边界。 */
function validateDecimalRange(
  value: { min?: string | undefined; max?: string | undefined },
  context: z.RefinementCtx,
): void {
  if (value.min === undefined && value.max === undefined) {
    context.addIssue({ code: 'custom', message: 'range requires min or max' });
    return;
  }
  if (value.min !== undefined && value.max !== undefined && Number(value.min) > Number(value.max)) {
    context.addIssue({ code: 'custom', message: 'range min must not exceed max' });
  }
}

/** 校验估值或资金流的包含端数值范围。 */
const decimalRangeSchema = z
  .object({
    min: decimalSchema.optional(),
    max: decimalSchema.optional(),
  })
  .strict()
  .superRefine(validateDecimalRange);

/** 校验查询采用的方法学身份；version 省略时由同步服务选择当前冻结版本。 */
const methodologyFilterSchema = z
  .object({
    code: z.string().min(1).max(120),
    version: z.string().min(1).max(80).optional(),
  })
  .strict();

/** 校验行业、概念或申万筛选项。 */
const membershipFilterSchema = z
  .object({
    scheme: z.enum(EQUITY_MEMBERSHIP_SCHEMES),
    code: z.string().min(1).max(80),
  })
  .strict();

/** 校验估值口径与数值范围。 */
const valuationFilterSchema = z
  .object({
    metric: z.enum(EQUITY_VALUATION_METRICS),
    methodology: methodologyFilterSchema,
    range: decimalRangeSchema,
  })
  .strict();

/** 校验日频主力资金流方法学、分桶与数值范围。 */
const moneyFlowFilterSchema = z
  .object({
    methodology: methodologyFilterSchema,
    bucket: z.enum(EQUITY_MONEY_FLOW_BUCKETS),
    range: decimalRangeSchema,
  })
  .strict();

/** 校验服务端稳定排序项；所有空值由下游固定置后。 */
const discoverySortSchema = z
  .object({
    field: z.enum(EQUITY_DISCOVERY_SORT_FIELDS),
    direction: z.enum(['ASC', 'DESC']),
  })
  .strict();

/** 校验股票中心搜索请求并拒绝所有未知字段。 */
export const equitySearchRequestSchema = z
  .object({
    q: z.preprocess(trimOptionalString, z.string().min(1).max(64)).optional(),
    exchanges: z.array(z.enum(EQUITY_EXCHANGES)).min(1).max(3).optional(),
    listingStatuses: z.array(z.enum(EQUITY_LISTING_STATUSES)).min(1).max(3).optional(),
    tradingStatuses: z
      .array(z.enum(EQUITY_TRADING_STATUSES))
      .min(1)
      .max(EQUITY_TRADING_STATUSES.length)
      .optional(),
    memberships: z.array(membershipFilterSchema).min(1).max(20).optional(),
    valuation: valuationFilterSchema.optional(),
    moneyFlow: moneyFlowFilterSchema.optional(),
    columns: z.array(z.enum(EQUITY_DISCOVERY_COLUMNS)).min(1).max(24).optional(),
    sort: z.array(discoverySortSchema).min(1).max(3).optional(),
    cursor: z.string().min(1).max(1024).optional(),
    limit: z.number().int().min(1).max(100).default(50),
    dataVersion: z.string().uuid().optional(),
  })
  .strict();

/** 校验统一证券事件查询请求并拒绝所有未知字段。 */
export const equityEventSearchRequestSchema = z
  .object({
    families: z.array(z.enum(EQUITY_EVENT_FAMILIES)).min(1).max(5).optional(),
    asOf: dateSchema.optional(),
    start: dateSchema,
    end: dateSchema,
    knownAt: dateTimeSchema.optional(),
    cursor: z.string().min(1).max(1024).optional(),
    limit: z.number().int().min(1).max(100).default(50),
  })
  .strict();

/** 校验详情页数据状态请求并拒绝所有未知字段。 */
export const equityDataStatusRequestSchema = z
  .object({
    families: z
      .array(z.enum(EQUITY_DATASET_FAMILIES))
      .min(1)
      .max(EQUITY_DATASET_FAMILIES.length)
      .optional(),
    asOf: dateSchema.optional(),
    knownAt: dateTimeSchema.optional(),
  })
  .strict();

/** 使用指定严格 schema 解析公开请求，并隐藏底层校验器结构。 */
export function parseEquityWorkspaceRequest<T>(schema: z.ZodType<T>, input: unknown): T {
  const parsed = schema.safeParse(input);
  if (parsed.success) return parsed.data;
  throw new PublicProblemException(400, 'validation-error', 'Equity workspace request is invalid');
}

/** 描述公开股票中心搜索请求 DTO。 */
export type EquitySearchRequestDto = z.infer<typeof equitySearchRequestSchema>;

/** 描述公开统一证券事件请求 DTO。 */
export type EquityEventSearchRequestDto = z.infer<typeof equityEventSearchRequestSchema>;

/** 描述公开详情数据状态请求 DTO。 */
export type EquityDataStatusRequestDto = z.infer<typeof equityDataStatusRequestSchema>;
