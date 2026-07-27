import { describe, expect, it } from "vite-plus/test";

import { safeReturnTo } from "../return-to";

describe("safeReturnTo", () => {
  /** 成功登录后保留相对的受保护目标地址。 */
  it("accepts a same-origin relative application path", () => {
    expect(safeReturnTo("/users?role=USER")).toBe("/users?role=USER");
  });

  /** 拒绝协议相对地址与外部地址，避免登录流程形成开放重定向。 */
  it("rejects unsafe redirect values", () => {
    expect(safeReturnTo("//example.invalid")).toBe("/");
    expect(safeReturnTo("https://example.invalid/users")).toBe("/");
  });
});
