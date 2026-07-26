import { Role, UserStatus } from '../../../generated/prisma/client.js';
import { Transform } from 'class-transformer';
import { IsIn, IsString, Length, ValidateIf } from 'class-validator';

export class UpdateUserDto {
  @ValidateIf((_object, value: unknown) => value !== undefined)
  @Transform(({ value }: { value: unknown }) => (typeof value === 'string' ? value.trim() : value))
  @IsString()
  @Length(1, 120)
  public readonly displayName?: string;

  @ValidateIf((_object, value: unknown) => value !== undefined)
  @IsIn([Role.USER, Role.ADMIN])
  public readonly role?: Role;

  @ValidateIf((_object, value: unknown) => value !== undefined)
  @IsIn([UserStatus.ACTIVE, UserStatus.DISABLED])
  public readonly status?: UserStatus;
}
