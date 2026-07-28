import { IsUUID } from 'class-validator';

import { EquityPathDto } from './equity-path.dto.js';

/** 约束一份公开财务报表的证券路径与不透明报表引用。 */
export class FinancialReportPathDto extends EquityPathDto {
  /** 指定公开安全的报表 UUID，不接受数据库主键。 */
  @IsUUID()
  public readonly reportRef!: string;
}
