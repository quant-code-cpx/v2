import { IsIn, Matches } from 'class-validator';

import { EQUITY_EXCHANGES } from '../equity-instrument.contract.js';

/** 约束公开证券路径中的交易所与六位代码。 */
export class EquityPathDto {
  /** 指定证券所属交易所。 */
  @IsIn(EQUITY_EXCHANGES)
  public readonly exchange!: (typeof EQUITY_EXCHANGES)[number];

  /** 指定交易所内六位证券代码。 */
  @Matches(/^[0-9]{6}$/)
  public readonly symbol!: string;
}
