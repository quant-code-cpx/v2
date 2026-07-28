import { Transform, Type } from 'class-transformer';
import { Role, UserStatus } from '../../../generated/prisma/client.js';
import { IsIn, IsInt, IsString, Max, MaxLength, Min, MinLength, ValidateIf } from 'class-validator';

export class ListUsersQueryDto {
  @ValidateIf((_object, value: unknown) => value !== undefined)
  @IsString()
  @MaxLength(512)
  public readonly cursor?: string;

  @ValidateIf((_object, value: unknown) => value !== undefined)
  @Transform(({ value }: { value: unknown }) => (typeof value === 'string' ? value.trim() : value))
  @IsString()
  @MinLength(1)
  @MaxLength(120)
  public readonly q?: string;

  @ValidateIf((_object, value: unknown) => value !== undefined)
  @IsIn([Role.USER, Role.ADMIN])
  public readonly role?: Role;

  @ValidateIf((_object, value: unknown) => value !== undefined)
  @IsIn([UserStatus.ACTIVE, UserStatus.DISABLED, UserStatus.DELETED])
  public readonly status?: UserStatus;

  @ValidateIf((_object, value: unknown) => value !== undefined)
  @IsIn(['createdAt', 'updatedAt', 'account', 'displayName'])
  public readonly sort: 'createdAt' | 'updatedAt' | 'account' | 'displayName' = 'createdAt';

  @ValidateIf((_object, value: unknown) => value !== undefined)
  @IsIn(['asc', 'desc'])
  public readonly order: 'asc' | 'desc' = 'desc';

  @ValidateIf((_object, value: unknown) => value !== undefined)
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(100)
  public readonly pageSize: number = 20;
}
