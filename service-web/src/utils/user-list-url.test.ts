import { describe, expect, it } from "vite-plus/test";

import { readUserDialogState, readUserListFilters } from "./user-list-url";

describe("user-management URL state", () => {
  /** Keep one stable table sort active even when a shared URL omits it. */
  it("normalizes omitted sort and order to created time descending", () => {
    expect(readUserListFilters(new URLSearchParams())).toEqual({
      sort: "createdAt",
      order: "desc",
      pageSize: 20,
    });
    expect(readUserListFilters(new URLSearchParams("order=asc"))).toEqual({
      sort: "createdAt",
      order: "asc",
      pageSize: 20,
    });
  });

  /** Parse shareable filters using frozen defaults and valid explicit values. */
  it("reads valid list filters from URL state", () => {
    const filters = readUserListFilters(
      new URLSearchParams(
        "q=market.user&role=USER&status=DISABLED&sort=account&order=asc&pageSize=50&cursor=next",
      ),
    );

    expect(filters).toEqual({
      q: "market.user",
      role: "USER",
      status: "DISABLED",
      sort: "account",
      order: "asc",
      pageSize: 50,
      cursor: "next",
    });
  });

  /** Reject malformed action target IDs so a URL cannot open an arbitrary mutation dialog. */
  it("rejects a malformed edit dialog target", () => {
    expect(
      readUserDialogState(new URLSearchParams("dialog=edit&userId=not-a-uuid")),
    ).toBeUndefined();
  });
});
