import { IsString, Matches, MaxLength, MinLength } from 'class-validator';

export class ResetPasswordDto {
  @IsString()
  @MinLength(12)
  @MaxLength(512)
  @Matches(/\d/, { message: 'password must contain a number' })
  public readonly password!: string;
}
