import { IsDateString, IsOptional } from 'class-validator';

/** 约束单板块 EOD 报价读取；日期缺省只表示 latest，不表示向前回退。 */
export class GetSectorEodSnapshotQueryDto {
  /** 指定精确交易日；缺失时读取该体系最新 published 快照。 */
  @IsOptional()
  @IsDateString({ strict: true })
  public readonly asOf?: string;
}
