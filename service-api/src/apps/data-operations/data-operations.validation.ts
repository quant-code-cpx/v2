import type { z } from 'zod';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';

/** 使用严格 Zod schema 校验公开请求，并保持合同定义的 400/422 分界。 */
export function validateDataOperationsRequest<T>(schema: z.ZodType<T>, input: unknown): T {
  const result = schema.safeParse(input);
  if (result.success) {
    return result.data;
  }
  const message = result.error.issues.map((issue) => issue.message).join('; ');
  // 重复 target、计划半空组合与数据集固定操作冲突都是业务语义错误，合同要求明确使用 422。
  if (
    message.includes('Duplicate datasetCode') ||
    message.includes('scheduleId and expectedVersion') ||
    message.includes('ETF datasetCode and selector operation do not match') ||
    message.includes('single ETF venue must match the qualified ETF identity')
  ) {
    throw new PublicProblemException(
      422,
      'unprocessable-content',
      'Data operations request is invalid',
    );
  }
  throw new PublicProblemException(400, 'validation-error', 'Data operations request is invalid');
}

/** 校验公开 Idempotency-Key，绝不 trim 或改写其原始隔离值。 */
export function validateIdempotencyKey(value: string | undefined): string {
  if (value === undefined || value.length < 16 || value.length > 128) {
    throw new PublicProblemException(400, 'validation-error', 'Idempotency-Key is required');
  }
  return value;
}
