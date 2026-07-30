import { describe, expect, it } from "vite-plus/test";

import { createNavigationGroups } from "../../components/AppShell/navigation";
import { createAppRouter } from "../index";

describe("equity workspace routes", () => {
  /** canonical 列表、详情和旧链接迁移必须同时存在于受保护路由树。 */
  it("registers list, canonical detail, and legacy migration routes", () => {
    const router = createAppRouter();
    const protectedRoot = router.routes.find(
      /** 受保护壳层以根路径为稳定父路由。 */
      (route) => route.path === "/",
    );
    const paths =
      protectedRoot?.children?.map(
        /** 只读取声明式路径，不执行需要会话的 loader。 */
        (route) => route.path,
      ) ?? [];
    router.dispose();

    expect(paths).toContain("market/equities");
    expect(paths).toContain("market/equities/:exchange/:symbol");
    expect(paths).toContain("instruments/:symbol");
  });

  /** 股票导航必须覆盖 canonical 详情，但不能继续归入市场概览。 */
  it("selects canonical equity children through the dedicated navigation item", () => {
    const marketGroup = createNavigationGroups().find(
      /** 市场分组承载所有公开市场发现入口。 */
      (group) => group.label === "市场",
    );
    const equityItem = marketGroup?.items.find(
      /** 股票中心使用唯一 canonical 列表目标。 */
      (item) => item.to === "/market/equities",
    );
    const overviewItem = marketGroup?.items.find(
      /** 市场概览不再接管个股详情前缀。 */
      (item) => item.to === "/market",
    );

    expect(equityItem?.activePrefixes).toEqual(["/market/equities"]);
    expect(overviewItem?.activePrefixes ?? []).not.toContain("/market/equities");
  });
});
