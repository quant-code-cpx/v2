import { Type } from 'class-transformer';
import {
  IsDateString,
  IsIn,
  IsInt,
  IsOptional,
  IsString,
  IsUUID,
  Length,
  Matches,
  Max,
  Min,
} from 'class-validator';

import {
  EQUITY_ADJUSTMENT_MODES,
  EQUITY_BAR_PERIODS,
} from '../../../data-sync/contracts/equity-market-data.contract.js';
import { DATE_ONLY_PATTERN } from './temporal-patterns.js';

/** 约束个股行情周期、日期、复权模式和页上限。 */
export class ListEquityBarsQueryDto {
  /** 绑定 data-status 返回的精确行情 publication，禁止状态与叶子数据静默混版。 */
  @IsUUID()
  public readonly dataVersion!: string;

  /** 复权时绑定 data-status 返回的精确因子 publication。 */
  @IsOptional()
  @IsUUID()
  public readonly factorDataVersion?: string;

  /** 选择一个上游独立物理周期。 */
  @IsOptional()
  @IsIn(EQUITY_BAR_PERIODS)
  public readonly period: (typeof EQUITY_BAR_PERIODS)[number] = '1d';

  /** 指定包含端窗口起始日期。 */
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly start!: string;

  /** 指定包含端窗口结束日期。 */
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly end!: string;

  /** 选择未复权、前复权或后复权。 */
  @IsOptional()
  @IsIn(EQUITY_ADJUSTMENT_MODES)
  public readonly adjust: (typeof EQUITY_ADJUSTMENT_MODES)[number] = 'none';

  /** 指定前复权锚点；省略时使用请求结束日。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly adjustAsOf?: string;

  /** 透传同一发布与查询范围上一页返回的防篡改游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 1024)
  public readonly cursor?: string;

  /** 限制单次行情最多两千条。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(2000)
  public readonly limit: number = 500;
}
