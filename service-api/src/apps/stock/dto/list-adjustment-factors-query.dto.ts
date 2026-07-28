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

/** 约束累计复权因子的日期窗口和页上限。 */
export class ListAdjustmentFactorsQueryDto {
  /** 可选包含端起始日期。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly start?: string;

  /** 必填包含端结束日期。 */
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly end!: string;

  /** 透传同一因子发布与日期范围上一页返回的防篡改游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 1024)
  public readonly cursor?: string;

  /** 限制单次最多五百个稀疏因子点。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(500)
  public readonly limit: number = 500;
}
