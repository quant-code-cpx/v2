import { IsString, Length } from 'class-validator';

export class UpdateProfileDto {
  @IsString()
  @Length(1, 120)
  public readonly displayName!: string;
}
