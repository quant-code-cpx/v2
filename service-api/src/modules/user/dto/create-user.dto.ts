import { Role, UserStatus } from '../../../generated/prisma/client.js';
import { IsEmail, IsEnum, IsOptional, IsString, Length, Matches, MinLength } from 'class-validator';

export class CreateUserDto {
  @IsEmail()
  public readonly email!: string;

  @IsString()
  @Length(1, 120)
  public readonly displayName!: string;

  @IsString()
  @MinLength(12)
  @Matches(/\d/, { message: 'password must contain a number' })
  public readonly password!: string;

  @IsOptional()
  @IsEnum(Role)
  public readonly role?: Role;

  @IsOptional()
  @IsEnum(UserStatus)
  public readonly status?: UserStatus;
}
