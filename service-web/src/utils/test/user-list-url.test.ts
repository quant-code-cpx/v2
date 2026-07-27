import { describe, expect, it } from "vite-plus/test";

import { readUserDialogState, readUserListFilters } from "../user-list-url";

describe("user-management URL state", () => {
  /** 共享 URL 未指定排序时仍保持稳定的表格排序。 */
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

  /** 使用冻结默认值与合法显式值解析可分享筛选条件。 */
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

  /** 拒绝格式错误的操作目标 ID，避免 URL 打开任意变更对话框。 */
  it("rejects a malformed edit dialog target", () => {
    expect(
      readUserDialogState(new URLSearchParams("dialog=edit&userId=not-a-uuid")),
    ).toBeUndefined();
  });
});
