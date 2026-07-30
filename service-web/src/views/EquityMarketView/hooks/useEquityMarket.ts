import { useCallback, useDeferredValue, useEffect, useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import type { ChangeEvent } from "react";

import { conditionalBody, equitySearchQueryOptions } from "../../../api/equity-market";
import { isApiError } from "../../../api/http";
import type {
  EquityExchange,
  EquityListingStatus,
  EquitySearchSortField,
  EquityTradingStatus,
} from "../../../types/equity-market";
import { readEquityMarketUrl, writeEquityMarketUrl } from "../utils/equity-market-url";
import type { EquityMarketUrlState } from "../utils/equity-market-url";

/** 描述一次筛选更新是否需要保留当前 cursor 页。 */
type EquityMarketUpdate = Partial<EquityMarketUrlState> & { preserveCursor?: boolean };

/** 将 URL 分类参数映射为服务端冻结的 membership scheme。 */
function buildMemberships(state: EquityMarketUrlState) {
  return [
    ...state.industries.map((code) => ({ scheme: "EASTMONEY_INDUSTRY" as const, code })),
    ...state.concepts.map((code) => ({ scheme: "EASTMONEY_CONCEPT" as const, code })),
    ...state.swIndustries.map((code) => ({ scheme: "SW2021_L3" as const, code })),
  ];
}

/** 管理股票中心可分享 URL、TanStack Query、快照恢复和页面动作。 */
export function useEquityMarket() {
  const [searchParams, setSearchParams] = useSearchParams();
  const state = useMemo(() => readEquityMarketUrl(searchParams), [searchParams]);
  const deferredQuery = useDeferredValue(state.q);
  const memberships = useMemo(() => buildMemberships(state), [state]);
  const request = useMemo(
    () => ({
      ...(deferredQuery === undefined ? {} : { q: deferredQuery }),
      ...(state.exchanges.length === 0 ? {} : { exchanges: state.exchanges }),
      ...(state.listingStatuses.length === 0 ? {} : { listingStatuses: state.listingStatuses }),
      ...(state.tradingStatuses.length === 0 ? {} : { tradingStatuses: state.tradingStatuses }),
      ...(memberships.length === 0 ? {} : { memberships }),
      sort: [{ field: state.sort, direction: state.order === "asc" ? "ASC" : "DESC" }] as const,
      ...(state.cursor === undefined ? {} : { cursor: state.cursor }),
      limit: state.limit,
      ...(state.dataVersion === undefined ? {} : { dataVersion: state.dataVersion }),
    }),
    [
      deferredQuery,
      memberships,
      state.cursor,
      state.dataVersion,
      state.exchanges,
      state.limit,
      state.listingStatuses,
      state.order,
      state.sort,
      state.tradingStatuses,
    ],
  );
  const searchQuery = useQuery(equitySearchQueryOptions(request));
  const response = conditionalBody(searchQuery.data);
  const recoveredCursorRef = useRef<string | undefined>(undefined);

  // 非法或冗余 URL 会被替换成规范形式，使复制、刷新和 Query key 始终一致。
  useEffect(() => {
    const normalized = writeEquityMarketUrl(state);
    if (normalized.toString() !== searchParams.toString()) {
      setSearchParams(normalized, { replace: true });
    }
  }, [searchParams, setSearchParams, state]);

  // snapshot-expired 只自动恢复一次；保留用户筛选并从新 publication 第一页重新开始。
  useEffect(() => {
    if (
      state.cursor === undefined ||
      !isApiError(searchQuery.error) ||
      searchQuery.error.status !== 409 ||
      searchQuery.error.code !== "snapshot-expired" ||
      recoveredCursorRef.current === state.cursor
    ) {
      return;
    }

    recoveredCursorRef.current = state.cursor;
    setSearchParams(
      writeEquityMarketUrl({ ...state, cursor: undefined, page: 1, dataVersion: undefined }),
      { replace: true },
    );
  }, [searchQuery.error, setSearchParams, state]);

  /** 更新一个或多个筛选；除翻页外都回到当前 publication 的第一页。 */
  const update = useCallback(
    (changes: EquityMarketUpdate) => {
      const { preserveCursor = false, ...values } = changes;
      const nextState = {
        ...state,
        ...values,
        ...(preserveCursor ? {} : { cursor: undefined, page: 1 }),
      };
      setSearchParams(writeEquityMarketUrl(nextState));
    },
    [setSearchParams, state],
  );

  /** 更新代码或名称检索词。 */
  const setQuery = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const q = event.target.value.trimStart().slice(0, 64);
      update({ q: q.length === 0 ? undefined : q });
    },
    [update],
  );

  /** 更新交易所多选。 */
  const setExchanges = useCallback(
    (values: EquityExchange[]) => update({ exchanges: values }),
    [update],
  );

  /** 更新上市生命周期多选。 */
  const setListingStatuses = useCallback(
    (values: EquityListingStatus[]) => update({ listingStatuses: values }),
    [update],
  );

  /** 更新普通交易状态多选。 */
  const setTradingStatuses = useCallback(
    (values: EquityTradingStatus[]) => update({ tradingStatuses: values }),
    [update],
  );

  /** 更新单个 Eastmoney 行业代码筛选。 */
  const setIndustry = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const value = event.target.value.trim();
      update({ industries: value.length === 0 ? [] : [value.slice(0, 64)] });
    },
    [update],
  );

  /** 更新单个 Eastmoney 概念代码筛选。 */
  const setConcept = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const value = event.target.value.trim();
      update({ concepts: value.length === 0 ? [] : [value.slice(0, 64)] });
    },
    [update],
  );

  /** 更新单个申万 2021 三级节点代码筛选。 */
  const setSwIndustry = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const value = event.target.value.trim();
      update({ swIndustries: value.length === 0 ? [] : [value.slice(0, 64)] });
    },
    [update],
  );

  /** 切换服务端排序字段并保留 null 固定 LAST 的契约语义。 */
  const setSort = useCallback(
    (field: EquitySearchSortField) => {
      const isCurrent = state.sort === field;
      update({
        sort: field,
        order: isCurrent ? (state.order === "asc" ? "desc" : "asc") : "desc",
      });
    },
    [state.order, state.sort, update],
  );

  /** 使用服务端 opaque cursor 前往同一 dataVersion 的下一页。 */
  const nextPage = useCallback(() => {
    const nextCursor = response?.page.nextCursor;
    if (nextCursor !== null && nextCursor !== undefined) {
      update({
        cursor: nextCursor,
        page: state.page + 1,
        dataVersion: response?.release?.dataVersion ?? state.dataVersion,
        preserveCursor: true,
      });
    }
  }, [
    response?.page.nextCursor,
    response?.release?.dataVersion,
    state.dataVersion,
    state.page,
    update,
  ]);

  /** 保留筛选并回到第一页；不猜测上一页 cursor。 */
  const firstPage = useCallback(() => {
    update({ cursor: undefined, page: 1, preserveCursor: true });
  }, [update]);

  /** 清除全部业务筛选和精确快照，只保留默认页大小。 */
  const reset = useCallback(() => {
    setSearchParams(writeEquityMarketUrl(readEquityMarketUrl(new URLSearchParams())));
  }, [setSearchParams]);

  /** 只复验当前可见列表 Query，不触发任何同步写操作。 */
  const refresh = useCallback(() => {
    void searchQuery.refetch();
  }, [searchQuery]);

  return {
    state,
    response,
    searchQuery,
    setQuery,
    setExchanges,
    setListingStatuses,
    setTradingStatuses,
    setIndustry,
    setConcept,
    setSwIndustry,
    setSort,
    nextPage,
    firstPage,
    reset,
    refresh,
  };
}

/** 暴露列表私有组件共享的页面模型类型。 */
export type EquityMarketModel = ReturnType<typeof useEquityMarket>;
