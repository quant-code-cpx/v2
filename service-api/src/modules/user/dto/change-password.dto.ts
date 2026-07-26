import { IsString, Matches, MaxLength, MinLength } from 'class-validator';

export class ChangePasswordDto {
  @IsString()
  @MinLength(1)
  @MaxLength(512)
  public readonly currentPassword!: string;

  @IsString()
  @MinLength(12)
  @MaxLength(512)
  @Matches(/\d/, { message: 'newPassword must contain a number' })
  public readonly newPassword!: string;
}
