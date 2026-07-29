import { z } from 'zod';

/** 表示可安全穿越服务边界的 JSON 对象，不允许值携带可执行结构。 */
const jsonObjectSchema = z.record(z.string(), z.unknown());

/** 定义公开端可转交给内部 typed reader 的单 dataset 查询请求骨架。 */
export const marketDataQueryRequestSchema = z
  .object({
    dataset: z.object({
      code: z.string().regex(/^[a-z][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)+$/),
      schemaVersion: z.number().int().positive(),
    }),
    businessScope: z.enum([
      'MARKET',
      'SECURITY',
      'INDEX',
      'ETF',
      'FUND',
      'CHANNEL',
      'REPORT',
      'EVENT',
      'CONTRACT',
    ]),
    time: jsonObjectSchema,
    visibility: jsonObjectSchema,
    selection: jsonObjectSchema,
    fields: z.array(z.string().min(1).max(120)).min(1).max(64),
    sort: z.array(jsonObjectSchema).min(1).max(3),
    identity: jsonObjectSchema.optional(),
    filters: z.array(jsonObjectSchema).max(64).optional(),
    page: jsonObjectSchema.optional(),
  })
  .strict();

/** 表示公开服务支持的 data-sync 市场查询请求。 */
export type MarketDataQueryRequest = z.infer<typeof marketDataQueryRequestSchema>;

/** 定义有 canonical publication 时不可变发布元数据的最小安全投影。 */
const availableReleaseSchema = z
  .object({
    dataVersion: z.string().uuid(),
    publishedAt: z.string().datetime({ offset: true }),
    knowledgeCutoff: z.string().datetime({ offset: true }),
    publicUsableAt: z.string().datetime({ offset: true }),
    effectiveFrom: z.string().datetime({ offset: true }).nullable(),
    effectiveTo: z.string().datetime({ offset: true }).nullable(),
    methodology: jsonObjectSchema,
    sources: z.array(jsonObjectSchema),
    quality: jsonObjectSchema,
    completeness: z.enum(['COMPLETE', 'PARTIAL', 'UNKNOWN']),
  })
  .passthrough();

/** 定义尚无可读取发布时的成功空结果元数据。 */
const emptyReleaseSchema = z
  .object({
    state: z.enum(['EMPTY', 'SOURCE_UNAVAILABLE']),
    observedAt: z.string().datetime({ offset: true }).nullable(),
    reasonCode: z.string().min(1).max(80),
  })
  .strict();

/** 定义 service-api 可以安全返回给前端的异构 typed-record 页面。 */
export const marketDataQueryResponseSchema = z
  .object({
    meta: z
      .object({
        requestId: z.string().uuid(),
        contractVersion: z.literal('1.0.0'),
        dataset: z.object({
          code: z.string(),
          schemaVersion: z.number().int().positive(),
        }),
        availability: z.enum(['AVAILABLE', 'EMPTY', 'SOURCE_UNAVAILABLE']),
        release: z.union([availableReleaseSchema, emptyReleaseSchema]),
        visibility: jsonObjectSchema,
        page: z.object({
          limit: z.number().int().positive().max(500),
          hasMore: z.boolean(),
          nextCursor: z.string().nullable(),
        }),
        coverage: jsonObjectSchema,
        warnings: z.array(z.string()),
        disclaimers: z.array(z.string()),
      })
      .strict(),
    records: z.array(jsonObjectSchema).max(500),
  })
  .strict();

/** 表示 service-api 公开市场数据查询的合同化响应。 */
export type MarketDataQueryResponse = z.infer<typeof marketDataQueryResponseSchema>;
