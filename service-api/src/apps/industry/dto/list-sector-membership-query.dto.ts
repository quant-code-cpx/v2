import { Type } from 'class-transformer';
import { IsInt, IsISO8601, IsOptional, IsString, Length, Max, Min } from 'class-validator';

/** 约束单板块成分页的 release 时刻、游标和页大小。 */
export class ListSectorMembershipQueryDto {
  /** 选择不晚于该 RFC 3339 时刻的固定观测 release，省略时使用当前 release。 */
  @IsOptional()
  @IsISO8601({ strict: true })
  public readonly asOf?: string;

  /** 透传来自同一 dataVersion 的不透明游标，切换 release 后必须从第一页重启。 */
  @IsOptional()
  @IsString()
  @Length(1, 1024)
  public readonly cursor?: string;

  /** 限制成员页大小，控制下游查询、内存和响应体上界。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(500)
  public readonly limit: number = 200;
}
