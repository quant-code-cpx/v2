import { describe, expect, it } from "vite-plus/test";

import { ApiError } from "../../../api/http";
import { discoverableListingStatus, shouldResetEventCursor } from "../hooks/useEquityDetail";

describe("useEquityDetail helpers", () => {
  /** 退市详情必须显式查询退市 discovery，暂停上市则保持未覆盖。 */
  it("keeps listed and delisted discovery states while rejecting suspended coverage", () => {
    expect(discoverableListingStatus("LISTED")).toBe("LISTED");
    expect(discoverableListingStatus("DELISTED")).toBe("DELISTED");
    expect(discoverableListingStatus("SUSPENDED")).toBeUndefined();
  });

  /** 只有带 cursor 的事件 snapshot-expired 才需要回到第一页。 */
  it("resets an event cursor only after the composite publication expires", () => {
    expect(shouldResetEventCursor(new ApiError(409, "snapshot-expired"), "opaque-cursor")).toBe(
      true,
    );
    expect(shouldResetEventCursor(new ApiError(409, "snapshot-expired"), undefined)).toBe(false);
    expect(
      shouldResetEventCursor(new ApiError(503, "dependency-unavailable"), "opaque-cursor"),
    ).toBe(false);
  });
});
