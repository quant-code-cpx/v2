import { Transform, Type } from 'class-transformer';
import { IsIn, IsInt, IsOptional, IsString, Length, Max, Min } from 'class-validator';

import { SECTOR_SCHEMES } from '../../../data-sync/contracts/sector-market-data.contract.js';

/** 约束公开板块目录查询的分类体系、前缀、游标和页大小。 */
export class ListSectorsQueryDto {
  /** 选择不可混用的行业或概念分类体系。 */
  @IsIn(SECTOR_SCHEMES)
  public readonly scheme!: (typeof SECTOR_SCHEMES)[number];

  /** 可选的代码或名称前缀，空白值在进入下游前被拒绝。 */
  @IsOptional()
  @IsString()
  @Length(1, 64)
  @Transform(({ value }: { value: unknown }) => (typeof value === 'string' ? value.trim() : value))
  public readonly query?: string;

  /** 透传自上一次响应的受约束不透明游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 1024)
  public readonly cursor?: string;

  /** 限制每页目录记录数，保护下游数据库与浏览器响应体。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(100)
  public readonly limit: number = 100;
}
