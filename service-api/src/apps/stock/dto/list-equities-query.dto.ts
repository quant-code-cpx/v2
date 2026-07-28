import { Transform, Type, type TransformFnParams } from 'class-transformer';
import {
  ArrayMaxSize,
  ArrayMinSize,
  ArrayUnique,
  IsArray,
  IsDateString,
  IsISO8601,
  IsIn,
  IsInt,
  IsOptional,
  IsString,
  Length,
  Matches,
  Max,
  Min,
} from 'class-validator';

import {
  EQUITY_EXCHANGES,
  EQUITY_LISTING_STATUSES,
} from '../../../data-sync/contracts/equity-instrument.contract.js';
import { DATE_ONLY_PATTERN, OFFSET_DATE_TIME_PATTERN } from './temporal-patterns.js';

/** 把单个或重复查询参数规范化为数组。 */
function toArray({ value }: TransformFnParams): unknown {
  if (value === undefined) return undefined;
  return Array.isArray(value) ? value : [value];
}

/** 去除查询前后空白，同时保留非字符串供校验器拒绝。 */
function trimString({ value }: TransformFnParams): unknown {
  return typeof value === 'string' ? value.trim() : value;
}

/** 约束证券目录的交易所、状态、点时时间、游标与页大小。 */
export class ListEquitiesQueryDto {
  /** 可选指定单一交易所；省略时读取稳定三所聚合发布。 */
  @IsOptional()
  @IsIn(EQUITY_EXCHANGES)
  public readonly exchange?: (typeof EQUITY_EXCHANGES)[number];

  /** 可选重复传入最多三个上市生命周期状态。 */
  @IsOptional()
  @Transform(toArray)
  @IsArray()
  @ArrayMinSize(1)
  @ArrayMaxSize(3)
  @ArrayUnique()
  @IsIn(EQUITY_LISTING_STATUSES, { each: true })
  public readonly status?: (typeof EQUITY_LISTING_STATUSES)[number][];

  /** 可选按当前时间切片中的代码或名称做前缀查询。 */
  @IsOptional()
  @Transform(trimString)
  @IsString()
  @Length(1, 64)
  public readonly query?: string;

  /** 选择业务有效日期。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly asOf?: string;

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

  /** 限制公开目录每页最多一百条。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(100)
  public readonly limit: number = 50;
}
