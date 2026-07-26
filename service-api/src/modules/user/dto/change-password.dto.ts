import { IsString, Matches, MinLength } from 'class-validator';

export class ChangePasswordDto {
  @IsString()
  public readonly currentPassword!: string;

  @IsString()
  @MinLength(12)
  @Matches(/\d/, { message: 'newPassword must contain a number' })
  public readonly newPassword!: string;
}
