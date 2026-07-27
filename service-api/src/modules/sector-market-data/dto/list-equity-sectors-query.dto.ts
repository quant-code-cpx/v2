import { IsIn } from 'class-validator';

import { SECTOR_SCHEMES } from '../sector-market-data.contract.js';
import { ListSectorMembershipQueryDto } from './list-sector-membership-query.dto.js';

/** 约束证券反向板块读取额外必须固定一个分类体系，避免混合不同 release。 */
export class ListEquitySectorsQueryDto extends ListSectorMembershipQueryDto {
  /** 指定行业或概念分类体系；同一响应只会选择该体系的一个固定 release。 */
  @IsIn(SECTOR_SCHEMES)
  public readonly scheme!: (typeof SECTOR_SCHEMES)[number];
}
