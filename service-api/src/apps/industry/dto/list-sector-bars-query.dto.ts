import { Type } from 'class-transformer';
import { IsDateString, IsIn, IsInt, IsOptional, IsString, Length, Max, Min } from 'class-validator';

/** 约束单板块三物理周期 K 线的有界读取请求。 */
export class ListSectorBarsQueryDto {
  /** 只允许独立同步的日线、周线、月线；没有分钟或派生周期。 */
  @IsIn(['1d', '1w', '1mo'])
  public readonly period!: '1d' | '1w' | '1mo';

  /** 指定包含端 ISO 开始日期。 */
  @IsDateString({ strict: true })
  public readonly start!: string;

  /** 指定包含端 ISO 结束日期。 */
  @IsDateString({ strict: true })
  public readonly end!: string;

  /** 透传自同一发布快照上一页的受约束不透明游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 1024)
  public readonly cursor?: string;

  /** 限制每页 K 线数量，避免无界历史响应。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(1000)
  public readonly limit: number = 1000;
}
