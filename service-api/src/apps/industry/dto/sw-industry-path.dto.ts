import { Matches } from 'class-validator';

/** 约束公开详情路径中的申万六位代码与 `.SI` 后缀。 */
export class SwIndustryPathDto {
  /** 申万行业稳定代码。 */
  @Matches(/^[0-9]{6}\.SI$/)
  public readonly code!: string;
}
