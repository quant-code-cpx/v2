import { IsInt, IsString, Max, MaxLength, Min, ValidateIf } from 'class-validator';

/** 描述本人活动会话族列表的有界分页输入。 */
export class SessionListDto {
  @ValidateIf(isProvided)
  @IsString()
  @MaxLength(512)
  public readonly cursor?: string;

  @ValidateIf(isProvided)
  @IsInt()
  @Min(1)
  @Max(50)
  public readonly pageSize: number = 20;
}

/** 仅在可选输入实际出现时运行后续校验器。 */
function isProvided(_object: object, value: unknown): boolean {
  return value !== undefined;
}
