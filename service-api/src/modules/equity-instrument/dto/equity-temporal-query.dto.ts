import { IsDateString, IsISO8601, IsOptional, Matches } from 'class-validator';

import { DATE_ONLY_PATTERN, OFFSET_DATE_TIME_PATTERN } from './temporal-patterns.js';

/** 约束证券详情按业务时间与知识时间读取。 */
export class EquityTemporalQueryDto {
  /** 选择证券身份和字段的业务有效日期。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly asOf?: string;

  /** 选择平台在该时刻已经知道的数据版本。 */
  @IsOptional()
  @Matches(OFFSET_DATE_TIME_PATTERN)
  @IsISO8601({ strict: true })
  public readonly knownAt?: string;
}
