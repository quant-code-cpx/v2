import { Type } from 'class-transformer';
import {
  IsDateString,
  IsISO8601,
  IsInt,
  IsOptional,
  IsString,
  Length,
  Matches,
  Max,
  Min,
} from 'class-validator';

import { DATE_ONLY_PATTERN, OFFSET_DATE_TIME_PATTERN } from './temporal-patterns.js';

/** 约束上市状态历史的身份时间、历史窗口和分页。 */
export class ListListingStatusHistoryQueryDto {
  /** 选择路径代码所属证券的业务有效日期。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly asOf?: string;

  /** 过滤生命周期区间的包含端起始日期。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly effectiveFrom?: string;

  /** 过滤生命周期区间的不包含端结束日期。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly effectiveTo?: string;

  /** 选择平台知识时刻。 */
  @IsOptional()
  @Matches(OFFSET_DATE_TIME_PATTERN)
  @IsISO8601({ strict: true })
  public readonly knownAt?: string;

  /** 透传同一不可变发布上一页返回的不透明游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 1024)
  public readonly cursor?: string;

  /** 限制公开历史每页最多一百条。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(100)
  public readonly limit: number = 50;
}
