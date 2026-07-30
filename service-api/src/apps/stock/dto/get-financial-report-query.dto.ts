import { Transform, Type } from 'class-transformer';
import {
  ArrayMaxSize,
  ArrayMinSize,
  ArrayUnique,
  IsArray,
  IsDateString,
  IsInt,
  IsISO8601,
  IsOptional,
  IsString,
  IsUUID,
  Length,
  Matches,
  Max,
  Min,
} from 'class-validator';

import { financialNumberType, toFinancialArray } from './financial-query-transforms.js';
import { DATE_ONLY_PATTERN, OFFSET_DATE_TIME_PATTERN } from './temporal-patterns.js';

/** 约束单份财务报表详情的字段筛选、双时态与分页范围。 */
export class GetFinancialReportQueryDto {
  /** 绑定报告列表返回的精确财务 publication。 */
  @IsUUID()
  public readonly dataVersion!: string;

  /** 可选筛选一至一百个治理字段代码。 */
  @IsOptional()
  @Transform(toFinancialArray)
  @IsArray()
  @ArrayMinSize(1)
  @ArrayMaxSize(100)
  @ArrayUnique()
  @IsString({ each: true })
  @Matches(/^[a-z][a-z0-9_.-]{1,79}$/, { each: true })
  public readonly metric?: string[];

  /** 选择业务有效日期。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly asOf?: string;

  /** 选择平台当时已知的时间切片。 */
  @IsOptional()
  @Matches(OFFSET_DATE_TIME_PATTERN)
  @IsISO8601({ strict: true })
  public readonly knownAt?: string;

  /** 透传绑定同一报表 revision 的不透明游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 1024)
  public readonly cursor?: string;

  /** 限制单页最多二百个治理字段。 */
  @IsOptional()
  @Type(financialNumberType)
  @IsInt()
  @Min(1)
  @Max(200)
  public readonly limit: number = 100;
}
