import { IsIn, IsString, Length } from 'class-validator';

import { SECTOR_SCHEMES } from '../sector-market-data.contract.js';

/** 约束板块路径中的稳定分类体系和代码，而不是展示名称。 */
export class SectorPathDto {
  /** 指定行业或概念分类体系。 */
  @IsIn(SECTOR_SCHEMES)
  public readonly scheme!: (typeof SECTOR_SCHEMES)[number];

  /** 指定分类体系内稳定板块代码。 */
  @IsString()
  @Length(1, 64)
  public readonly sectorCode!: string;
}
