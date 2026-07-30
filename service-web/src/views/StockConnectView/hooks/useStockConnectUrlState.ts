import { useCallback, useEffect, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import {
  parseStockConnectSecurityUrlState,
  parseStockConnectUrlState,
  serializeStockConnectChannelDetailUrlState,
  serializeStockConnectSecurityUrlState,
  serializeStockConnectUrlState,
} from "../utils/stock-connect-url";
import type {
  StockConnectChannelSlug,
  StockConnectSecurityUrlState,
  StockConnectUrlState,
} from "../utils/stock-connect-url";

/** 描述筛选更新时的浏览器历史与游标策略。 */
interface StockConnectUrlUpdateOptions {
  replace?: boolean;
  preserveCursor?: boolean;
}

/** 描述总览或固定 path 通道两种 URL 所有权。 */
interface StockConnectUrlStateOptions {
  fixedChannel?: StockConnectChannelSlug;
}

/** 管理总览和通道详情的规范 URL 状态，筛选变化默认回到第一页。 */
export function useStockConnectUrlState(options: StockConnectUrlStateOptions = {}) {
  const [searchParameters, setSearchParameters] = useSearchParams();

  /** 读取未知查询串并应用冻结默认值与枚举边界。 */
  const parsedState = useMemo(
    () => parseStockConnectUrlState(searchParameters),
    [searchParameters],
  );
  const state = useMemo(
    /** 固定通道详情以 path 为唯一事实源，忽略查询串中的冲突通道和方向。 */
    () =>
      options.fixedChannel === undefined
        ? parsedState
        : { ...parsedState, direction: "all" as const, channel: options.fixedChannel },
    [options.fixedChannel, parsedState],
  );
  const serializeState = useCallback(
    /** 单通道详情不重复序列化 path 已持有的通道，其他页面保留完整筛选。 */
    (nextState: StockConnectUrlState) =>
      options.fixedChannel === undefined
        ? serializeStockConnectUrlState(nextState)
        : serializeStockConnectChannelDetailUrlState(nextState),
    [options.fixedChannel],
  );
  const canonicalSearch = useMemo(
    /** 将当前规范状态转换为浏览器查询串。 */
    () => serializeState(state).toString(),
    [serializeState, state],
  );

  /** 首次遇到未知或非法参数时只替换当前历史项，保证复制链接可复现。 */
  useEffect(() => {
    if (searchParameters.toString() !== canonicalSearch) {
      setSearchParameters(canonicalSearch, { replace: true });
    }
  }, [canonicalSearch, searchParameters, setSearchParameters]);

  /** 合并筛选并写入 URL；除翻页外全部清除旧 publication 的游标。 */
  const update = useCallback(
    (patch: Partial<StockConnectUrlState>, options: StockConnectUrlUpdateOptions = {}) => {
      const nextState = { ...state, ...patch };
      if (options.preserveCursor !== true) {
        delete nextState.cursor;
      }
      setSearchParameters(serializeState(nextState), {
        replace: options.replace,
      });
    },
    [serializeState, setSearchParameters, state],
  );

  /** 清除游标并回到当前筛选的第一页。 */
  const goToFirstPage = useCallback(() => {
    const nextState = { ...state };
    delete nextState.cursor;
    setSearchParameters(serializeState(nextState));
  }, [serializeState, setSearchParameters, state]);

  /** 以 replace 清除跨 publication 失效游标，避免浏览器后退重新进入失效页。 */
  const recoverStaleCursor = useCallback(() => {
    const nextState = { ...state };
    delete nextState.cursor;
    setSearchParameters(serializeState(nextState), { replace: true });
  }, [serializeState, setSearchParameters, state]);

  /** 写入服务端返回的不透明下一游标，不解析其内部内容。 */
  const goToNextPage = useCallback(
    (cursor: string) => {
      update({ cursor }, { preserveCursor: true });
    },
    [update],
  );

  return {
    state,
    update,
    goToFirstPage,
    goToNextPage,
    recoverStaleCursor,
  };
}

/** 管理证券上下文页的规范 URL 日期、可选通道和历史窗口。 */
export function useStockConnectSecurityUrlState() {
  const [searchParameters, setSearchParameters] = useSearchParams();

  /** 读取证券上下文日期、可选通道与交易日窗口。 */
  const state = useMemo(
    () => parseStockConnectSecurityUrlState(searchParameters),
    [searchParameters],
  );

  /** 将证券上下文筛选转换为规范可分享查询串。 */
  const canonicalSearch = useMemo(
    () => serializeStockConnectSecurityUrlState(state).toString(),
    [state],
  );

  /** 删除未知参数并把非法值替换为冻结默认值。 */
  useEffect(() => {
    if (searchParameters.toString() !== canonicalSearch) {
      setSearchParameters(canonicalSearch, { replace: true });
    }
  }, [canonicalSearch, searchParameters, setSearchParameters]);

  /** 合并证券上下文筛选并保留浏览器前进后退语义。 */
  const update = useCallback(
    (patch: Partial<StockConnectSecurityUrlState>) => {
      setSearchParameters(serializeStockConnectSecurityUrlState({ ...state, ...patch }));
    },
    [setSearchParameters, state],
  );

  return { state, update };
}
