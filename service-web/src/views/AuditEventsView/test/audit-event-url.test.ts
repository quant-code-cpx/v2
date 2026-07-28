import { describe, expect, it } from "vite-plus/test";

import {
  parseAuditUrlState,
  serializeAuditUrlState,
  toAuditListInput,
} from "../utils/audit-event-url";

/** 固定 URL 测试使用的 UUID。 */
const actorId = "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415";

describe("audit event URL", () => {
  /** 规范状态可稳定往返，并省略最近七天默认值。 */
  it("round-trips canonical filters and selected detail", () => {
    const parameters = serializeAuditUrlState({
      category: "ACCOUNT",
      range: "7d",
      actorId,
      includeRoutine: true,
      cursor: "next-cursor",
      eventId: actorId,
    });

    expect(parameters.has("range")).toBe(false);
    expect(parseAuditUrlState(parameters)).toEqual({
      category: "ACCOUNT",
      range: "7d",
      actorId,
      includeRoutine: true,
      cursor: "next-cursor",
      eventId: actorId,
    });
  });

  /** 无效枚举与标识不会进入 Query 或 API 请求。 */
  it("drops invalid filters and applies the seven-day default", () => {
    const state = parseAuditUrlState(
      new URLSearchParams("category=UNKNOWN&range=forever&actorId=unsafe&eventId=unsafe"),
    );

    expect(state).toEqual({ range: "7d", includeRoutine: false });
  });

  /** 固定范围按一次请求时间转换为精确 ISO 窗口。 */
  it("creates a stable 24-hour API window", () => {
    const input = toAuditListInput(
      { range: "24h", includeRoutine: false },
      new Date("2026-07-28T06:00:00.000Z"),
    );

    expect(input).toEqual({
      occurredFrom: "2026-07-27T06:00:00.000Z",
      occurredTo: "2026-07-28T06:00:00.000Z",
      includeRoutine: false,
      pageSize: 20,
    });
  });
});
