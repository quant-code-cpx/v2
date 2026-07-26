import { IsString, IsUUID, MaxLength, MinLength } from 'class-validator';

export class LoginDto {
  @IsString()
  @MinLength(1)
  @MaxLength(32)
  public readonly account!: string;

  @IsString()
  @MinLength(1)
  @MaxLength(512)
  public readonly password!: string;

  @IsUUID()
  public readonly captchaId!: string;

  @IsString()
  @MinLength(1)
  @MaxLength(16)
  public readonly captchaAnswer!: string;
}
