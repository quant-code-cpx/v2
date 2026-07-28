import { Type } from 'class-transformer';
import { IsDateString, IsIn, IsInt, IsOptional, IsString, Length, Max, Min } from 'class-validator';

import {
  SECTOR_EOD_SORTS,
  SECTOR_SCHEMES,
} from '../../../data-sync/contracts/sector-market-data.contract.js';

/** 约束 EOD 横截面排行请求，所有游标必须绑定同一发布版本和排序语义。 */
export class ListSectorEodSnapshotsQueryDto {
  /** 指定不可混用的行业或概念分类体系。 */
  @IsIn(SECTOR_SCHEMES)
  public readonly scheme!: (typeof SECTOR_SCHEMES)[number];

  /** 请求精确交易日；缺省时由同步服务选择该体系最新 published 快照。 */
  @IsOptional()
  @IsDateString({ strict: true })
  public readonly asOf?: string;

  /** 指定确定性动态排行字段，禁止把供应商默认排名暴露为产品事实。 */
  @IsOptional()
  @IsIn(SECTOR_EOD_SORTS)
  public readonly sort: (typeof SECTOR_EOD_SORTS)[number] = 'changePercent';

  /** 指定排序方向；null 的相对位置始终由同步服务固定为最后。 */
  @IsOptional()
  @IsIn(['asc', 'desc'])
  public readonly order: 'asc' | 'desc' = 'desc';

  /** 透传同一 dataVersion 的签名不透明游标，禁止客户端拆解或构造位置。 */
  @IsOptional()
  @IsString()
  @Length(1, 1024)
  public readonly cursor?: string;

  /** 限制每页横截面数量，避免无界下游扫描和公开响应。 */
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(500)
  public readonly limit: number = 100;
}
