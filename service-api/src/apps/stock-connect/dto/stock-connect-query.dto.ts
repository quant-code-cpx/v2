import { Type } from 'class-transformer';
import {
  ArrayMaxSize,
  ArrayMinSize,
  ArrayUnique,
  IsArray,
  IsDefined,
  IsIn,
  IsInt,
  IsString,
  Max,
  MaxLength,
  Min,
  MinLength,
  Validate,
  ValidateIf,
  ValidateNested,
  ValidatorConstraint,
  type ValidationArguments,
  type ValidatorConstraintInterface,
} from 'class-validator';

import {
  STOCK_CONNECT_CHANNELS,
  STOCK_CONNECT_RANKINGS,
  isSafeStockConnectDataVersion,
  stockConnectDateSelectionSchema,
} from '../../../data-sync/contracts/stock-connect.contract.js';

/** 仅在字段不是显式 null 时继续执行字符串或枚举验证。 */
function isNotNull(_object: object, value: unknown): boolean {
  return value !== null;
}

/** 在 class-validator 边界校验日期模式与 exactDate 的互斥关系。 */
@ValidatorConstraint({ name: 'stockConnectDateSelection', async: false })
class StockConnectDateSelectionConstraint implements ValidatorConstraintInterface {
  /** 使用共享 Zod 合同一次校验整个嵌套日期对象。 */
  public validate(_value: unknown, arguments_: ValidationArguments): boolean {
    return stockConnectDateSelectionSchema.safeParse(arguments_.object).success;
  }

  /** 返回不泄露实现细节的稳定字段错误。 */
  public defaultMessage(): string {
    return 'date mode and exactDate do not match';
  }
}

/** 在公开 DTO 边界拒绝版本标识中的控制字符，但不把版本格式误限定为 UUID。 */
@ValidatorConstraint({ name: 'stockConnectDataVersion', async: false })
class StockConnectDataVersionConstraint implements ValidatorConstraintInterface {
  /** 复用服务间合同的版本字符校验，确保 DTO 与 Zod 入口一致。 */
  public validate(value: unknown): boolean {
    return typeof value === 'string' && isSafeStockConnectDataVersion(value);
  }
}

/** 约束 latest 与 exact 查询都显式携带互斥日期状态。 */
export class StockConnectDateSelectionDto {
  /** 指定使用最新已完成 publication 或精确交易日。 */
  @IsIn(['LATEST', 'EXACT'])
  public readonly mode!: 'LATEST' | 'EXACT';

  /** exact 模式携带严格交易日期；latest 模式必须显式为 null。 */
  @Validate(StockConnectDateSelectionConstraint)
  public readonly exactDate!: string | null;
}

/** 约束互联互通总览请求的日期、通道集合和趋势窗口。 */
export class StockConnectOverviewQueryDto {
  /** 选择共同 publication 的 latest 或 exact 日期语义。 */
  @IsDefined()
  @ValidateNested()
  @Type(() => StockConnectDateSelectionDto)
  public readonly date!: StockConnectDateSelectionDto;

  /** 选择一至四条互不重复的业务通道。 */
  @IsArray()
  @ArrayMinSize(1)
  @ArrayMaxSize(4)
  @ArrayUnique()
  @IsIn(STOCK_CONNECT_CHANNELS, { each: true })
  public readonly channels!: Array<(typeof STOCK_CONNECT_CHANNELS)[number]>;

  /** 限制返回的交易日趋势点数量。 */
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(250)
  public readonly trendTradingDays!: number;
}

/** 约束候选交易日和逐通道 readiness 查询。 */
export class StockConnectReadinessQueryDto {
  /** 选择最新候选日或一个绝不回退的精确业务日期。 */
  @IsDefined()
  @ValidateNested()
  @Type(() => StockConnectDateSelectionDto)
  public readonly date!: StockConnectDateSelectionDto;

  /** 选择一至四条互不重复、需要共同解释准备状态的通道。 */
  @IsArray()
  @ArrayMinSize(1)
  @ArrayMaxSize(4)
  @ArrayUnique()
  @IsIn(STOCK_CONNECT_CHANNELS, { each: true })
  public readonly channels!: Array<(typeof STOCK_CONNECT_CHANNELS)[number]>;
}

/** 约束单通道详情请求。 */
export class StockConnectChannelQueryDto {
  /** 选择该通道的 latest 或 exact publication。 */
  @IsDefined()
  @ValidateNested()
  @Type(() => StockConnectDateSelectionDto)
  public readonly date!: StockConnectDateSelectionDto;

  /** 指定一条不可拆分的交易所与方向组合。 */
  @IsIn(STOCK_CONNECT_CHANNELS)
  public readonly channel!: (typeof STOCK_CONNECT_CHANNELS)[number];

  /** 限制返回的交易日趋势点数量。 */
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(250)
  public readonly trendTradingDays!: number;
}

/** 约束官方活跃证券榜和仅在榜内的净额排序请求。 */
export class StockConnectActiveSecurityQueryDto {
  /** 选择该通道榜单的 latest 或 exact publication。 */
  @IsDefined()
  @ValidateNested()
  @Type(() => StockConnectDateSelectionDto)
  public readonly date!: StockConnectDateSelectionDto;

  /** 指定一条不可拆分的交易所与方向组合。 */
  @IsIn(STOCK_CONNECT_CHANNELS)
  public readonly channel!: (typeof STOCK_CONNECT_CHANNELS)[number];

  /** 选择来源活跃次序或来源榜内可用的净额排序。 */
  @IsIn(STOCK_CONNECT_RANKINGS)
  public readonly ranking!: (typeof STOCK_CONNECT_RANKINGS)[number];

  /** 绑定发起榜单查询时已展示的父 overview 或 channel publication。 */
  @IsString()
  @MinLength(1)
  @MaxLength(160)
  @Validate(StockConnectDataVersionConstraint)
  public readonly parentPublicationDataVersion!: string;

  /** 传递绑定 publication、通道与排序的不透明游标，首屏必须显式为 null。 */
  @ValidateIf(isNotNull)
  @IsDefined()
  @IsString()
  @MaxLength(1024)
  public readonly cursor!: string | null;

  /** 限制单页最多返回一百条记录。 */
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(100)
  public readonly limit!: number;
}

/** 约束稳定证券引用在互联互通范围内的历史查询。 */
export class StockConnectSecurityContextQueryDto {
  /** 指定服务间已经解析的稳定证券实体引用，不接受来源代码替代。 */
  @IsString()
  @MinLength(1)
  @MaxLength(160)
  public readonly instrumentEntityRef!: string;

  /** 选择证券上下文的 latest 或 exact publication。 */
  @IsDefined()
  @ValidateNested()
  @Type(() => StockConnectDateSelectionDto)
  public readonly date!: StockConnectDateSelectionDto;

  /** 可选收窄到一条通道；查询全部通道时必须显式为 null。 */
  @ValidateIf(isNotNull)
  @IsDefined()
  @IsIn(STOCK_CONNECT_CHANNELS)
  public readonly channel!: (typeof STOCK_CONNECT_CHANNELS)[number] | null;

  /** 限制最多读取二百五十个交易日。 */
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(250)
  public readonly historyTradingDays!: number;
}
