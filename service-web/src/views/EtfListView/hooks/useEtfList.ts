import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { etfProfileListQueryOptions } from "../../../api/etfs";
import { isApiError } from "../../../api/http";
import type { EtfListFilters } from "../../../types/etf";
import {
  readEtfListUrlState,
  resetEtfListCursor,
  writeEtfListUrlState,
} from "../utils/etf-list-url";

/** ETF 列表 Hook 暴露的远程查询与 URL 动作。 */
export interface UseEtfListResult {
  filters: EtfListFilters;
  query: ReturnType<typeof useQuery<ReturnType<typeof etfProfileListQueryOptions>>>;
  applyFilters: (
    changes: Partial<Pick<EtfListFilters, "exchange" | "q" | "sort" | "order">>,
  ) => void;
  goToNextPage: (cursor: string) => void;
  restartPagination: () => void;
  resetFilters: () => void;
  cursorRecoveryNotice: boolean;
  dismissCursorRecoveryNotice: () => void;
}

/** 让 ETF 列表远程状态归 TanStack Query、可分享状态归 URL。 */
export function useEtfList() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [cursorRecoveryNotice, setCursorRecoveryNotice] = useState(false);
  const filters = useMemo(
    /** URL 变化后重新读取白名单状态。 */
    () => readEtfListUrlState(searchParams),
    [searchParams],
  );
  const query = useQuery(etfProfileListQueryOptions(filters));

  /** publication 变化或伪造 cursor 被拒绝时自动回首页，避免重复发送不可恢复的旧 URL。 */
  useEffect(() => {
    const cursorRejected =
      isApiError(query.error) &&
      (query.error.status === 409 ||
        (query.error.status === 400 && query.error.code === "validation-error"));
    if (filters.cursor === undefined || !cursorRejected) {
      return;
    }

    setCursorRecoveryNotice(true);
    setSearchParams(
      writeEtfListUrlState({
        ...filters,
        cursor: undefined,
        page: 1,
      }),
      { replace: true },
    );
  }, [filters, query.error, setSearchParams]);

  /** 应用真实筛选或排序，并清除不再属于当前请求指纹的 cursor。 */
  function applyFilters(
    changes: Partial<Pick<EtfListFilters, "exchange" | "q" | "sort" | "order">>,
  ): void {
    setSearchParams(writeEtfListUrlState(resetEtfListCursor(filters, changes)));
  }

  /** 使用服务端 opaque cursor 进入下一页，并把页码和 cursor 一起写入 URL。 */
  function goToNextPage(cursor: string): void {
    setSearchParams(
      writeEtfListUrlState({
        ...filters,
        cursor,
        page: filters.page + 1,
      }),
    );
  }

  /** 从任意游标页回到相同筛选条件的第一页。 */
  function restartPagination(): void {
    setSearchParams(
      writeEtfListUrlState({
        ...filters,
        cursor: undefined,
        page: 1,
      }),
    );
  }

  /** 恢复 ETF 列表默认交易所、排序和无筛选首页。 */
  function resetFilters(): void {
    setSearchParams(new URLSearchParams());
  }

  /** 用户确认目录 publication 已变化后关闭本次恢复提示，后续恢复仍会重新显示。 */
  function dismissCursorRecoveryNotice(): void {
    setCursorRecoveryNotice(false);
  }

  return {
    filters,
    query,
    applyFilters,
    goToNextPage,
    restartPagination,
    resetFilters,
    cursorRecoveryNotice,
    dismissCursorRecoveryNotice,
  };
}
