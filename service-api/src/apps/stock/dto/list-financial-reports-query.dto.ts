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
  Length,
  Matches,
  Max,
  Min,
} from 'class-validator';

import {
  FINANCIAL_PERIOD_BASES,
  FINANCIAL_STATEMENT_SCOPES,
  FINANCIAL_STATEMENT_TYPES,
} from '../../../data-sync/contracts/financial-data.contract.js';
import { financialNumberType, toFinancialArray } from './financial-query-transforms.js';
import { DATE_ONLY_PATTERN, OFFSET_DATE_TIME_PATTERN } from './temporal-patterns.js';

/** 约束财务报表列表的方法学、筛选、双时态与分页范围。 */
export class ListFinancialReportsQueryDto {
  /** 可选筛选一至三种报表类型。 */
  @IsOptional()
  @Transform(toFinancialArray)
  @IsArray()
  @ArrayMinSize(1)
  @ArrayMaxSize(3)
  @ArrayUnique()
  @IsIn(FINANCIAL_STATEMENT_TYPES, { each: true })
  public readonly statementType?: (typeof FINANCIAL_STATEMENT_TYPES)[number][];

  /** 可选筛选一至四种期间口径。 */
  @IsOptional()
  @Transform(toFinancialArray)
  @IsArray()
  @ArrayMinSize(1)
  @ArrayMaxSize(4)
  @ArrayUnique()
  @IsIn(FINANCIAL_PERIOD_BASES, { each: true })
  public readonly basis?: (typeof FINANCIAL_PERIOD_BASES)[number][];

  /** 可选筛选报表合并范围。 */
  @IsOptional()
  @IsIn(FINANCIAL_STATEMENT_SCOPES)
  public readonly scope?: (typeof FINANCIAL_STATEMENT_SCOPES)[number];

  /** 显式选择唯一方法学代码。 */
  @IsString()
  @Matches(/^[a-z][a-z0-9_.-]{2,79}$/)
  public readonly methodologyCode!: string;

  /** 显式选择不可变方法学版本。 */
  @Type(financialNumberType)
  @IsInt()
  @Min(1)
  public readonly methodologyVersion!: number;

  /** 可选筛选报告期起点。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly reportPeriodFrom?: string;

  /** 可选筛选报告期终点。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly reportPeriodTo?: string;

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

  /** 透传绑定同一发布和筛选的不透明游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 1024)
  public readonly cursor?: string;

  /** 限制报表列表单页最多五十条。 */
  @IsOptional()
  @Type(financialNumberType)
  @IsInt()
  @Min(1)
  @Max(50)
  public readonly limit: number = 20;
}
