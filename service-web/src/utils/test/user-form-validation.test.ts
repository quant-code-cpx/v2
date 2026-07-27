import { describe, expect, it } from "vite-plus/test";

import { validateCreateUserInput } from "../user-form-validation";

describe("validateCreateUserInput", () => {
  /** 验证仅创建用户界面执行账号长度 5–32 位约束。 */
  it("rejects a short account while accepting a contract-valid initial password", () => {
    const errors = validateCreateUserInput({
      account: "abcd",
      displayName: "测试用户",
      password: "secure-pass-123",
      role: "USER",
      status: "ACTIVE",
    });

    expect(errors.account).toBeDefined();
    expect(errors.password).toBeUndefined();
  });

  /** 验证规范化后的合法账号、显示名和密码通过本地创建校验。 */
  it("accepts a valid contract-shaped create request", () => {
    const errors = validateCreateUserInput({
      account: "market.user",
      displayName: "测试用户",
      password: "secure-pass-123",
      role: "ADMIN",
      status: "DISABLED",
    });

    expect(errors).toEqual({});
  });
});
