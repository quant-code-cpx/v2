import { IsEmail, IsString, MaxLength, MinLength } from 'class-validator';

export class LoginDto {
  @IsEmail()
  @MaxLength(320)
  public readonly email!: string;

  @IsString()
  @MinLength(1)
  @MaxLength(512)
  public readonly password!: string;
}
