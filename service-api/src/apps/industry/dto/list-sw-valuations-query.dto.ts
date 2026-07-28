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

/** 约束申万估值的精确日期、层级和 HMAC 游标分页。 */
export class ListSwValuationsQueryDto {
  /** 选择精确已发布估值快照；省略时读取最新日期。 */
  @IsOptional()
  @IsDateString({ strict: true })
  @Matches(/^\d{4}-\d{2}-\d{2}$/)
  public readonly snapshotDate?: string;

  /** 可选限制为一个申万行业层级。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(3)
  public readonly level?: number;

  /** 延续同一估值发布与层级筛选的不透明 HMAC 游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 1024)
  public readonly cursor?: string;

  /** 限制单页最多 500 条行业估值。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(500)
  public readonly limit: number = 100;
}
