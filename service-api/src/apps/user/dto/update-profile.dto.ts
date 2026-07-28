import { Transform } from 'class-transformer';
import { IsString, Length } from 'class-validator';

export class UpdateProfileDto {
  @Transform(({ value }: { value: unknown }) => (typeof value === 'string' ? value.trim() : value))
  @IsString()
  @Length(1, 120)
  public readonly displayName!: string;
}
