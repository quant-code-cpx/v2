import { describe, expect, it } from "vite-plus/test";

import { validateCreateUserInput } from "./user-form-validation";

describe("validateCreateUserInput", () => {
  /** Confirm 5–32 account validation is enforced by creation UI only. */
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

  /** Confirm normalized valid account, display name, and password pass local creation validation. */
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
