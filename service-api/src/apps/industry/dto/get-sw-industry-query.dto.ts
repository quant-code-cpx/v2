import { IsDateString, IsOptional, Matches } from 'class-validator';

/** 约束单申万节点读取的可选精确快照日期。 */
export class GetSwIndustryQueryDto {
  /** 选择精确已发布 taxonomy；省略时读取最新日期。 */
  @IsOptional()
  @IsDateString({ strict: true })
  @Matches(/^\d{4}-\d{2}-\d{2}$/)
  public readonly snapshotDate?: string;
}
