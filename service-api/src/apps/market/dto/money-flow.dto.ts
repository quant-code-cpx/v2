import { Type } from 'class-transformer';
import {
  IsDateString,
  IsIn,
  IsInt,
  IsISO8601,
  IsOptional,
  IsString,
  Length,
  Matches,
  Max,
  Min,
} from 'class-validator';

import {
  MONEY_FLOW_PUBLIC_STATUSES,
  MONEY_FLOW_RANKING_SCOPE_TYPES,
  MONEY_FLOW_RANKING_WINDOWS,
  MONEY_FLOW_SCOPE_TYPES,
  MONEY_FLOW_SEMANTIC_FAMILIES,
} from '../../../data-sync/contracts/money-flow.contract.js';

const DATE_ONLY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const OFFSET_DATE_TIME_PATTERN =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

/** 约束所有资金流值路径必须显式选择方法学。 */
export class MoneyFlowMethodologyPathDto {
  /** 选择稳定方法学公开标识。 */
  @IsString()
  @Matches(/^[a-z][a-z0-9_.-]{2,99}$/)
  public readonly methodologyId!: string;
}

/** 约束证券资金流路径的交易所和六位代码。 */
export class EquityMoneyFlowPathDto extends MoneyFlowMethodologyPathDto {
  /** 指定证券交易所。 */
  @IsIn(['SSE', 'SZSE', 'BSE'])
  public readonly exchange!: 'SSE' | 'SZSE' | 'BSE';

  /** 指定六位来源代码，内部仍按事实日期解析永久身份。 */
  @Matches(/^[0-9]{6}$/)
  public readonly symbol!: string;
}

/** 约束板块资金流路径必须固定分类体系和板块代码。 */
export class SectorMoneyFlowPathDto extends MoneyFlowMethodologyPathDto {
  /** 指定板块分类体系，禁止隐式跨体系 fallback。 */
  @IsString()
  @Length(1, 64)
  public readonly scheme!: string;

  /** 指定分类体系内稳定板块代码。 */
  @IsString()
  @Length(1, 64)
  public readonly sectorCode!: string;
}

/** 约束市场资金流路径的上游 scope 代码。 */
export class MarketMoneyFlowPathDto extends MoneyFlowMethodologyPathDto {
  /** 指定来源报告的市场 scope，不由证券求和。 */
  @Matches(/^[a-z][a-z0-9_-]{1,31}$/)
  public readonly marketCode!: string;
}

/** 约束公开方法学目录的筛选和分页。 */
export class ListMoneyFlowMethodologiesQueryDto {
  /** 可选筛选交易方向或订单规模语义族。 */
  @IsOptional()
  @IsIn(MONEY_FLOW_SEMANTIC_FAMILIES)
  public readonly semanticFamily?: (typeof MONEY_FLOW_SEMANTIC_FAMILIES)[number];

  /** 可选筛选公开状态，省略时应用层固定 validated。 */
  @IsOptional()
  @IsIn(MONEY_FLOW_PUBLIC_STATUSES)
  public readonly methodologyStatus?: (typeof MONEY_FLOW_PUBLIC_STATUSES)[number];

  /** 可选筛选方法学支持的 canonical scope。 */
  @IsOptional()
  @IsIn(MONEY_FLOW_SCOPE_TYPES)
  public readonly scopeType?: (typeof MONEY_FLOW_SCOPE_TYPES)[number];

  /** 透传绑定目录 publication 与筛选的不透明签名游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 2048)
  public readonly cursor?: string;

  /** 限制目录单页大小。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(100)
  public readonly limit: number = 50;
}

/** 约束三类来源日序列共有的方法学、窗口和分页参数。 */
export class ListMoneyFlowDailyQueryDto {
  /** 选择不可变方法学版本字符串。 */
  @IsString()
  @Length(1, 64)
  public readonly methodologyVersion!: string;

  /** 选择版本内明确资金分桶。 */
  @Matches(/^[a-z][a-z0-9_-]{0,63}$/)
  public readonly bucket!: string;

  /** 指定包含端起始日期。 */
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly start!: string;

  /** 指定包含端结束日期。 */
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly end!: string;

  /** 可选选择平台当时已知的时间切片。 */
  @IsOptional()
  @Matches(OFFSET_DATE_TIME_PATTERN)
  @IsISO8601({ strict: true })
  public readonly knownAt?: string;

  /** 透传绑定同一序列、版本、窗口与知识时点的签名游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 2048)
  public readonly cursor?: string;

  /** 限制日序列单页大小。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(500)
  public readonly limit: number = 200;
}

/** 约束供应商排行的完整强身份和分页参数。 */
export class ListMoneyFlowRankingQueryDto {
  /** 选择不可变方法学版本字符串。 */
  @IsString()
  @Length(1, 64)
  public readonly methodologyVersion!: string;

  /** 选择证券或板块排行，市场 scope 不支持排行。 */
  @IsIn(MONEY_FLOW_RANKING_SCOPE_TYPES)
  public readonly scopeType!: (typeof MONEY_FLOW_RANKING_SCOPE_TYPES)[number];

  /** 选择供应商声明的唯一 universe。 */
  @Matches(/^[a-z][a-z0-9_.-]{1,99}$/)
  public readonly universe!: string;

  /** 选择供应商单日或滚动窗口。 */
  @IsIn(MONEY_FLOW_RANKING_WINDOWS)
  public readonly windowType!: (typeof MONEY_FLOW_RANKING_WINDOWS)[number];

  /** 指定窗口大小，并由服务校验与窗口类型匹配。 */
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(252)
  public readonly windowSize!: number;

  /** 选择供应商用于排序的明确 bucket。 */
  @Matches(/^[a-z][a-z0-9_-]{0,63}$/)
  public readonly bucket!: string;

  /** 可选选择 exact 快照日期，省略则读取 latest。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly tradeDate?: string;

  /** 透传绑定不可变快照和筛选的签名游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 2048)
  public readonly cursor?: string;

  /** 限制排行单页位置数量。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(500)
  public readonly limit: number = 100;
}
