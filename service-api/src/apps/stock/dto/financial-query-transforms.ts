import type { TransformFnParams } from 'class-transformer';

/** 把单个或重复财务查询参数规范化为数组。 */
export function toFinancialArray({ value }: TransformFnParams): unknown {
  if (value === undefined) return undefined;
  return Array.isArray(value) ? value : [value];
}

/** 告知 class-transformer 使用 Number 构造器解析整型查询参数。 */
export function financialNumberType(): NumberConstructor {
  return Number;
}
