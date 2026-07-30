import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { ApiError } from "../../../api/http";
import { queryClient } from "../../../api/query-client";
import { useStockConnectParentPublicationRecovery } from "../hooks/useStockConnectQueries";

/** 每个用例结束后恢复共享查询客户端和函数替身。 */
afterEach(() => {
  vi.restoreAllMocks();
  queryClient.clear();
});

/** 验证父 publication 漂移时的原子复核与有限自动重试。 */
describe("parent publication recovery", () => {
  /** 同一业务范围只自动成对复核一次，持续冲突时转为显式失败。 */
  it("invalidates the parent and active query once before exposing a conflict", async () => {
    const parentQueryKey = ["stock-connect", "overview", "actor", "request"] as const;
    const activeQueryKey = ["stock-connect", "active", "actor", "bundle-v1"] as const;
    const invalidateQueries = vi
      .spyOn(queryClient, "invalidateQueries")
      .mockResolvedValue(undefined);
    const activeError = new ApiError(409, "PARENT_PUBLICATION_MISMATCH");

    /** 渲染 publication 原子复核 Hook，并保持服务端持续返回相同冲突。 */
    const { result, rerender } = renderHook(
      /** 提供稳定业务范围，使自动重试上限可以被精确验证。 */
      () =>
        useStockConnectParentPublicationRecovery({
          activeError,
          activeSucceeded: false,
          parentQueryKey,
          activeQueryKey,
          scopeKey: "overview:latest:northbound",
        }),
    );

    await waitFor(
      /** 等待两次失效完成并确认持续冲突已经进入人工复核状态。 */
      () => {
        expect(invalidateQueries).toHaveBeenCalledTimes(2);
        expect(result.current.status).toBe("exhausted");
      },
    );
    expect(invalidateQueries).toHaveBeenNthCalledWith(1, {
      queryKey: parentQueryKey,
      exact: true,
      refetchType: "active",
    });
    expect(invalidateQueries).toHaveBeenNthCalledWith(2, {
      queryKey: activeQueryKey,
      exact: true,
      refetchType: "active",
    });

    rerender();
    rerender();
    expect(invalidateQueries).toHaveBeenCalledTimes(2);

    await act(
      /** 用户手动重试仍必须刷新父 publication 与活跃榜这一完整组合。 */
      async () => {
        result.current.retryPublicationPair();
      },
    );
    await waitFor(
      /** 等待人工成对重试的两个查询失效都完成。 */
      () => {
        expect(invalidateQueries).toHaveBeenCalledTimes(4);
      },
    );
  });
});
