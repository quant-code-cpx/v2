import { Transform, Type } from 'class-transformer';
import {
  ArrayMaxSize,
  ArrayMinSize,
  ArrayUnique,
  IsArray,
  IsDateString,
  IsIn,
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

import { FINANCIAL_VALUATION_METRICS } from '../../../data-sync/contracts/financial-data.contract.js';
import { financialNumberType, toFinancialArray } from './financial-query-transforms.js';
import { DATE_ONLY_PATTERN, OFFSET_DATE_TIME_PATTERN } from './temporal-patterns.js';

/** 约束估值方法学、指标集合、日期窗、双时态与分页范围。 */
export class ListValuationsQueryDto {
  /** 绑定 data-status 返回的精确估值 publication。 */
  @IsUUID()
  public readonly dataVersion!: string;

  /** 显式选择唯一估值方法学代码。 */
  @IsString()
  @Matches(/^[a-z][a-z0-9_.-]{2,79}$/)
  public readonly methodologyCode!: string;

  /** 显式选择不可变方法学版本。 */
  @Type(financialNumberType)
  @IsInt()
  @Min(1)
  public readonly methodologyVersion!: number;

  /** 必须选择一至五个受控估值指标。 */
  @Transform(toFinancialArray)
  @IsArray()
  @ArrayMinSize(1)
  @ArrayMaxSize(5)
  @ArrayUnique()
  @IsIn(FINANCIAL_VALUATION_METRICS, { each: true })
  public readonly metric!: (typeof FINANCIAL_VALUATION_METRICS)[number][];

  /** 指定包含端估值窗口起始日期。 */
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly start!: string;

  /** 指定包含端估值窗口结束日期。 */
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly end!: string;

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

  /** 透传绑定同一发布和估值窗口的不透明游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 1024)
  public readonly cursor?: string;

  /** 限制估值页最多一千条。 */
  @IsOptional()
  @Type(financialNumberType)
  @IsInt()
  @Min(1)
  @Max(1000)
  public readonly limit: number = 500;
}
