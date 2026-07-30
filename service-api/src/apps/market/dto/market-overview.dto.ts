import { Type } from 'class-transformer';
import {
  ArrayMaxSize,
  ArrayMinSize,
  ArrayUnique,
  IsArray,
  IsDateString,
  IsIn,
  IsInt,
  IsOptional,
  IsString,
  Length,
  Matches,
  Max,
  Min,
} from 'class-validator';

const DATE_ONLY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

/** 约束市场首页只能选择一个精确交易日或 latest 完整包。 */
export class MarketOverviewBodyDto {
  /** 选择精确交易日；省略时读取 latest complete bundle。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly asOf?: string;
}

/** 约束公开指数路径使用稳定身份，不接受名称或自由文本。 */
export class MarketIndexPathDto {
  /** 指定四个已发布主要指数之一，拒绝任意供应商代码越过身份映射。 */
  @IsIn(['sse-composite', 'szse-component', 'csi-300', 'chinext'])
  public readonly indexId!: 'sse-composite' | 'szse-component' | 'csi-300' | 'chinext';
}

/** 复用行情 K 线的包含端日期窗口与签名分页约束。 */
class MarketBarsWindowBodyDto {
  /** 指定包含端起始交易日。 */
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly start!: string;

  /** 指定包含端结束交易日。 */
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly end!: string;

  /** 延续同一指数、窗口和 publication 的不透明游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 2_048)
  public readonly cursor?: string;

  /** 限制单页最多返回 1,000 个交易日。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(1_000)
  public readonly limit: number = 250;
}

/** 约束指数日 K 线查询；指数周月线不在本模块请求时聚合。 */
export class ListMarketIndexBarsBodyDto extends MarketBarsWindowBodyDto {
  /** P0 只开放供应商来源日线，不由 API 聚合周线或月线。 */
  @IsIn(['1d'])
  public readonly period = '1d' as const;
}

/** 约束冻结全市场横截面证券排行的指标、方向和分页。 */
export class ListMarketEquityRankingsBodyDto {
  /** 选择精确交易日；省略时读取 latest complete ranking publication。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly asOf?: string;

  /** 选择明确的涨跌、成交额或换手率排序指标。 */
  @IsIn(['changePercent', 'amountCny', 'turnoverPercent'])
  public readonly metric!: 'changePercent' | 'amountCny' | 'turnoverPercent';

  /** 选择升序或降序，不使用隐式默认来改变强弱语义。 */
  @IsIn(['asc', 'desc'])
  public readonly order!: 'asc' | 'desc';

  /** 延续同一横截面、指标与排序方向的不透明游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 2_048)
  public readonly cursor?: string;

  /** 限制市场摘要每页最多 50 只证券。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(50)
  public readonly limit: number = 20;
}

/** 约束订单规模方法学证券资金流排行的一侧方向和分页。 */
export class ListMarketEquityMoneyFlowRankingsBodyDto {
  /** 选择精确交易日；省略时读取 latest complete ranking publication。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly asOf?: string;

  /** 明确读取净流入或净流出侧，供应商语义不会被描述为统一市场事实。 */
  @IsIn(['inflow', 'outflow'])
  public readonly direction!: 'inflow' | 'outflow';

  /** 延续同一方法学、日期与方向的不透明游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 2_048)
  public readonly cursor?: string;

  /** 限制市场摘要每页最多 50 只证券。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(50)
  public readonly limit: number = 20;
}

/** 约束沪深交易日历与会话日程的场所和包含端窗口。 */
export class QueryMarketCalendarBodyDto {
  /** 指定一个或两个交易场所，禁止用空列表隐式代表全市场。 */
  @IsArray()
  @ArrayMinSize(1)
  @ArrayMaxSize(2)
  @ArrayUnique()
  @IsIn(['SSE', 'SZSE'], { each: true })
  public readonly venues!: ('SSE' | 'SZSE')[];

  /** 指定包含端起始日历日期。 */
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly start!: string;

  /** 指定包含端结束日历日期。 */
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly end!: string;
}

/** 约束一个板块体系内独立发布的强弱与持续性排行。 */
export class ListMarketSectorStrengthBodyDto {
  /** 固定东财行业或概念体系，两套结果不合并排名。 */
  @IsIn(['eastmoney.industry', 'eastmoney.concept'])
  public readonly scheme!: 'eastmoney.industry' | 'eastmoney.concept';

  /** 选择精确交易日；省略时读取该体系 latest publication。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly asOf?: string;

  /** 选择单日、五日或二十日已冻结强弱方法学窗口。 */
  @Type(() => Number)
  @IsInt()
  @IsIn([1, 5, 20])
  public readonly window!: 1 | 5 | 20;

  /** 选择同一 publication 内的排名方向。 */
  @IsIn(['asc', 'desc'])
  public readonly order!: 'asc' | 'desc';

  /** 延续同一体系、日期、窗口和方向的不透明游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 2_048)
  public readonly cursor?: string;

  /** 限制单页最多返回 100 个板块。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(100)
  public readonly limit: number = 50;
}

/** 约束一个东财板块体系内来源资金流排行的日期、方向和分页。 */
export class ListMarketSectorMoneyFlowRankingsBodyDto {
  /** 固定东财行业或概念体系，两套来源结果不得合并排名。 */
  @IsIn(['eastmoney.industry', 'eastmoney.concept'])
  public readonly scheme!: 'eastmoney.industry' | 'eastmoney.concept';

  /** 选择精确交易日；省略时读取该体系 latest complete publication。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly asOf?: string;

  /** 选择净流入优先或净流出优先，排序只使用来源直报净额。 */
  @IsIn(['asc', 'desc'])
  public readonly order!: 'asc' | 'desc';

  /** 延续同一体系、日期、排序和 publication 的不透明游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 2_048)
  public readonly cursor?: string;

  /** 限制单页最多返回 100 个板块。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(100)
  public readonly limit: number = 50;
}

/** 约束申万行业已同步发布的日、周、月 K 线及日期窗口。 */
export class ListSwIndustryBarsBodyDto extends MarketBarsWindowBodyDto {
  /** 选择同步阶段已物化的周期；API 请求线程禁止临时聚合。 */
  @IsIn(['1d', '1w', '1mo'])
  public readonly period!: '1d' | '1w' | '1mo';
}

/** 约束申万正式成分 publication 的日期与签名分页。 */
export class ListSwIndustryConstituentsBodyDto {
  /** 选择精确成分快照日期；省略时读取 latest 正式 publication。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly asOf?: string;

  /** 延续同一行业和 publication 的不透明游标。 */
  @IsOptional()
  @IsString()
  @Length(1, 2_048)
  public readonly cursor?: string;

  /** 限制正式成分页最多返回 100 只证券。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(100)
  public readonly limit: number = 100;
}

/** 约束申万单节点估值只能选择精确日期或 latest publication。 */
export class GetSwIndustryValuationBodyDto {
  /** 选择精确估值交易日；省略时读取 latest 正式 publication。 */
  @IsOptional()
  @Matches(DATE_ONLY_PATTERN)
  @IsDateString({ strict: true })
  public readonly asOf?: string;
}
