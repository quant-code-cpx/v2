import { Role, UserStatus } from '../../../generated/prisma/client.js';
import { Transform } from 'class-transformer';
import { IsIn, IsString, Length, Matches, MaxLength, MinLength, ValidateIf } from 'class-validator';

export class CreateUserDto {
  @Transform(({ value }: { value: unknown }) =>
    typeof value === 'string' ? value.trim().toLowerCase() : value,
  )
  @IsString()
  @Length(5, 32)
  @Matches(/^[a-z0-9][a-z0-9._-]{4,31}$/)
  public readonly account!: string;

  @Transform(({ value }: { value: unknown }) => (typeof value === 'string' ? value.trim() : value))
  @IsString()
  @Length(1, 120)
  public readonly displayName!: string;

  @IsString()
  @MinLength(12)
  @MaxLength(512)
  @Matches(/\d/, { message: 'password must contain a number' })
  public readonly password!: string;

  @ValidateIf((_object, value: unknown) => value !== undefined)
  @IsIn([Role.USER, Role.ADMIN])
  public readonly role?: Role;

  @ValidateIf((_object, value: unknown) => value !== undefined)
  @IsIn([UserStatus.ACTIVE, UserStatus.DISABLED])
  public readonly status?: UserStatus;
}
