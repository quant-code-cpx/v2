import { useQuery } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import {
  auditEventDetailQueryOptions,
  auditEventListQueryOptions,
} from "../../../api/audit-events";
import {
  parseAuditUrlState,
  serializeAuditUrlState,
  toAuditListInput,
} from "../utils/audit-event-url";
import type { AuditUrlState } from "../utils/audit-event-url";

/** 管理审计 URL 筛选、游标、详情 Drawer 与远程 Query。 */
export function useAuditEvents() {
  const [searchParameters, setSearchParameters] = useSearchParams();
  const urlState = useMemo(() => parseAuditUrlState(searchParameters), [searchParameters]);
  /** 每次筛选变化只冻结一个请求时间，避免普通重渲染持续改变 Query key。 */
  const listInput = useMemo(() => toAuditListInput(urlState), [urlState]);
  const listQuery = useQuery(auditEventListQueryOptions(listInput));
  const detailQuery = useQuery({
    ...auditEventDetailQueryOptions(urlState.eventId ?? ""),
    enabled: urlState.eventId !== undefined,
  });

  /** 应用新筛选并回到第一页，同时关闭可能已过期的详情。 */
  const applyFilters = useCallback(
    (
      nextFilters: Pick<
        AuditUrlState,
        "category" | "range" | "actorId" | "targetId" | "includeRoutine"
      >,
    ) => {
      setSearchParameters(
        serializeAuditUrlState({
          range: nextFilters.range,
          includeRoutine: nextFilters.includeRoutine,
          ...(nextFilters.category === undefined ? {} : { category: nextFilters.category }),
          ...(nextFilters.actorId === undefined ? {} : { actorId: nextFilters.actorId }),
          ...(nextFilters.targetId === undefined ? {} : { targetId: nextFilters.targetId }),
        }),
      );
    },
    [setSearchParameters],
  );

  /** 清除全部筛选与游标，恢复最近七天默认范围。 */
  const resetFilters = useCallback(() => {
    setSearchParameters(new URLSearchParams());
  }, [setSearchParameters]);

  /** 把选中事件写入 URL，允许浏览器返回关闭 Drawer。 */
  const openEvent = useCallback(
    (eventId: string) => {
      setSearchParameters(
        serializeAuditUrlState({
          ...urlState,
          eventId,
        }),
      );
    },
    [setSearchParameters, urlState],
  );

  /** 从 URL 移除详情事件并保留当前筛选与游标。 */
  const closeEvent = useCallback(() => {
    const { eventId: _eventId, ...rest } = urlState;
    setSearchParameters(serializeAuditUrlState(rest), { replace: true });
  }, [setSearchParameters, urlState]);

  /** 应用服务端下一游标并关闭当前详情。 */
  const goToNextPage = useCallback(() => {
    const nextCursor = listQuery.data?.page.nextCursor;

    if (nextCursor === null || nextCursor === undefined) {
      return;
    }

    const { eventId: _eventId, ...rest } = urlState;
    setSearchParameters(serializeAuditUrlState({ ...rest, cursor: nextCursor }));
  }, [listQuery.data?.page.nextCursor, setSearchParameters, urlState]);

  /** 移除游标回到当前筛选的第一页。 */
  const goToFirstPage = useCallback(() => {
    const { cursor: _cursor, eventId: _eventId, ...rest } = urlState;
    setSearchParameters(serializeAuditUrlState(rest));
  }, [setSearchParameters, urlState]);

  /** 手动刷新列表时保留现有行。 */
  const refresh = useCallback(async () => {
    await listQuery.refetch();
  }, [listQuery]);

  return {
    urlState,
    listQuery,
    detailQuery,
    applyFilters,
    resetFilters,
    openEvent,
    closeEvent,
    goToNextPage,
    goToFirstPage,
    refresh,
  };
}
