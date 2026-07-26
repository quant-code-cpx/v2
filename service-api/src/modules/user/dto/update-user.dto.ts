import { Role, UserStatus } from '../../../generated/prisma/client.js';
import { IsEnum, IsOptional, IsString, Length } from 'class-validator';

export class UpdateUserDto {
  @IsOptional()
  @IsString()
  @Length(1, 120)
  public readonly displayName?: string;

  @IsOptional()
  @IsEnum(Role)
  public readonly role?: Role;

  @IsOptional()
  @IsEnum(UserStatus)
  public readonly status?: UserStatus;
}
