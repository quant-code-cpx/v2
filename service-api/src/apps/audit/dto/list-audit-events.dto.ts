import {
  IsBoolean,
  IsDateString,
  IsIn,
  IsInt,
  IsString,
  IsUUID,
  Max,
  MaxLength,
  Min,
  ValidateIf,
} from 'class-validator';

import type { AuditCategory } from '../audit.types.js';

const AUDIT_CATEGORIES: readonly AuditCategory[] = [
  'AUTHENTICATION',
  'ACCOUNT',
  'USER_ADMINISTRATION',
  'SYSTEM',
];

/** 描述超级管理员审计列表的筛选、时间窗与分页输入。 */
export class ListAuditEventsDto {
  @ValidateIf(isProvided)
  @IsIn(AUDIT_CATEGORIES)
  public readonly category?: AuditCategory;

  @ValidateIf(isProvided)
  @IsUUID('4')
  public readonly actorId?: string;

  @ValidateIf(isProvided)
  @IsUUID('4')
  public readonly targetId?: string;

  @ValidateIf(isProvided)
  @IsDateString({ strict: true })
  public readonly occurredFrom?: string;

  @ValidateIf(isProvided)
  @IsDateString({ strict: true })
  public readonly occurredTo?: string;

  @ValidateIf(isProvided)
  @IsBoolean()
  public readonly includeRoutine: boolean = false;

  @ValidateIf(isProvided)
  @IsString()
  @MaxLength(512)
  public readonly cursor?: string;

  @ValidateIf(isProvided)
  @IsInt()
  @Min(1)
  @Max(100)
  public readonly pageSize: number = 20;
}

/** 仅在可选输入实际出现时运行后续校验器。 */
function isProvided(_object: object, value: unknown): boolean {
  return value !== undefined;
}
