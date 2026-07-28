import { Type } from 'class-transformer';
import {
  IsDateString,
  IsInt,
  IsOptional,
  IsString,
  Length,
  Matches,
  Max,
  Min,
} from 'class-validator';

import { DATE_ONLY_PATTERN } from './temporal-patterns.js';

/** 约束公司行动报告期窗口与页上限。 */
export class ListCorporateActionsQueryDto {
  /** 可选包含端报告期起始日期。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly start?: string;

  /** 可选包含端报告期结束日期。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly end?: string;

  /** 透传同一事件发布与日期范围上一页返回的防篡改游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 1024)
  public readonly cursor?: string;

  /** 限制单次最多一百条事件。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(100)
  public readonly limit: number = 100;
}
