import { describe, expect, it } from "vite-plus/test";

import { parseAccountDialogState, serializeAccountDialogState } from "../utils/account-url";

/** 固定 Session family URL 测试使用的 UUID。 */
const familyId = "7ce0f18a-9f4d-4b3a-ae69-d0ff1707df91";

describe("account URL", () => {
  /** Session 撤销 Dialog 可由 URL 安全重访。 */
  it("round-trips one valid session family dialog", () => {
    const parameters = serializeAccountDialogState({ kind: "revoke-session", familyId });

    expect(parseAccountDialogState(parameters)).toEqual({
      kind: "revoke-session",
      familyId,
    });
  });

  /** 无效 family 标识不能打开安全动作。 */
  it("rejects an invalid family identifier", () => {
    expect(
      parseAccountDialogState(new URLSearchParams("dialog=revoke-session&familyId=../../unsafe")),
    ).toBeUndefined();
  });
});
