import { IsDateString, IsIn, IsISO8601, IsOptional, IsUUID, Matches } from 'class-validator';

import { SECTOR_SCHEMES } from '../../../data-sync/contracts/sector-market-data.contract.js';
import { ListSectorMembershipQueryDto } from './list-sector-membership-query.dto.js';

const DATE_ONLY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const OFFSET_DATE_TIME_PATTERN = /(?:Z|[+-]\d{2}:\d{2})$/;

/** 约束证券反向板块读取额外必须固定一个分类体系，避免混合不同 release。 */
export class ListEquitySectorsQueryDto extends ListSectorMembershipQueryDto {
  /** 指定行业或概念分类体系；同一响应只会选择该体系的一个固定 release。 */
  @IsIn(SECTOR_SCHEMES)
  public readonly scheme!: (typeof SECTOR_SCHEMES)[number];

  /** 精确选择数据状态给出的成分 publication，禁止叶查询静默前进到另一版本。 */
  @IsUUID()
  public readonly dataVersion!: string;

  /** 独立选择路由代码所属永久证券的业务日期，不能由成分 release 日期替代。 */
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly identityAsOf!: string;

  /** 独立选择平台解析证券身份时的知识时刻，必须包含明确时区偏移。 */
  @IsOptional()
  @Matches(OFFSET_DATE_TIME_PATTERN)
  @IsISO8601({ strict: true })
  public readonly knownAt?: string;
}
