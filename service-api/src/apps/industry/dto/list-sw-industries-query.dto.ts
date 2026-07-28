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

/** 约束申万 taxonomy 发布日期、层级、父级和 HMAC 游标分页。 */
export class ListSwIndustriesQueryDto {
  /** 选择精确已发布快照；省略时读取最新日期。 */
  @IsOptional()
  @IsDateString({ strict: true })
  @Matches(/^\d{4}-\d{2}-\d{2}$/)
  public readonly snapshotDate?: string;

  /** 可选限制为申万一级、二级或三级。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(3)
  public readonly level?: number;

  /** 可选限制为一个直接父级的子节点。 */
  @IsOptional()
  @Matches(/^[0-9]{6}\.SI$/)
  public readonly parentCode?: string;

  /** 延续同一 dataVersion 和筛选范围的不透明 HMAC 游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 1024)
  public readonly cursor?: string;

  /** 限制单页最多 500 个行业节点。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(500)
  public readonly limit: number = 100;
}
