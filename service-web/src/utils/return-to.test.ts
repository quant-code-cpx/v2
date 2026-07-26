import { describe, expect, it } from "vite-plus/test";

import { safeReturnTo } from "./return-to";

describe("safeReturnTo", () => {
  /** Preserve a relative protected target across one successful login. */
  it("accepts a same-origin relative application path", () => {
    expect(safeReturnTo("/users?role=USER")).toBe("/users?role=USER");
  });

  /** Reject protocol-relative and external paths so login cannot become an open redirect. */
  it("rejects unsafe redirect values", () => {
    expect(safeReturnTo("//example.invalid")).toBe("/");
    expect(safeReturnTo("https://example.invalid/users")).toBe("/");
  });
});
