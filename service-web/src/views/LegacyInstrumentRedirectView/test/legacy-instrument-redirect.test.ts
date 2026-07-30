import { describe, expect, it } from "vite-plus/test";

import { legacyEquityCanonicalTarget } from "../LegacyInstrumentRedirectView";

describe("legacy equity canonical target", () => {
  /** 旧链接只解析一次目录 publication，并把其身份日期固定到 canonical URL。 */
  it("pins the catalog effectiveAsOf in the redirect target", () => {
    expect(legacyEquityCanonicalTarget("SSE", "600519", "2026-07-29")).toBe(
      "/market/equities/SSE/600519?asOf=2026-07-29",
    );
  });
});
