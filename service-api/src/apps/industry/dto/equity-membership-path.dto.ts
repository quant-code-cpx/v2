import { IsIn, IsString, Matches } from 'class-validator';

import { EQUITY_EXCHANGES } from '../../../data-sync/contracts/equity-instrument.contract.js';

/** 约束证券到板块观察查询使用公开交易所和六位代码，而非内部 UUID。 */
export class EquityMembershipPathDto {
  /** 指定已发布证券身份所属的交易所。 */
  @IsIn(EQUITY_EXCHANGES)
  public readonly exchange!: (typeof EQUITY_EXCHANGES)[number];

  /** 指定六位 A 股代码；具体历史身份由同步服务 release 视图解析。 */
  @IsString()
  @Matches(/^[0-9]{6}$/)
  public readonly symbol!: string;
}
