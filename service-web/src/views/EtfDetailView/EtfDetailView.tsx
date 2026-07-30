import { lazy, Suspense, useMemo } from "react";
import { ArrowBackRounded as ArrowBackRoundedIcon } from "@mui/icons-material";
import { Alert, Box, Button, Skeleton, Stack, Typography } from "@mui/material";
import { Link as RouterLink } from "react-router-dom";

import { MarketDataPublication } from "../../components/MarketDataPublication";
import { MarketDataStateView } from "../../components/MarketDataStateView";
import { EtfDatasetSection } from "./components/EtfDatasetSection";
import type { EtfDatasetSectionState } from "./components/EtfDatasetSection";
import { EtfLatestValues } from "./components/EtfLatestValues";
import { EtfProfileSummary } from "./components/EtfProfileSummary";
import { EtfStatusGrid } from "./components/EtfStatusGrid";
import { useEtfDetail } from "./hooks/useEtfDetail";
import { createEtfNavPricePoints, latestByDate, toEtfCandles } from "./utils/etf-detail";
import {
  etfAvailabilityState,
  etfExchangeLabel,
  publicationDateMismatch,
  unavailableReleaseSummary,
} from "../../utils/etf-presentation";
import type { MarketDataPage } from "../../types/etf";

/** ETF 详情需要时才加载 KLineChart 引擎面板。 */
const KlinePanel = lazy(async () => {
  const { KlinePanel: Component } = await import("../InstrumentAnalysisView/components/KlinePanel");

  return { default: Component };
});

/** ETF 价格与 NAV 比较需要时才加载 ECharts 面板。 */
const EtfNavPriceChart = lazy(async () => {
  const { EtfNavPriceChart: Component } = await import("./components/EtfNavPriceChart");

  return { default: Component };
});

/** ETF 详情 K 线固定日周期，稳定引用避免普通重渲染重置图表数据。 */
const etfDailyPeriod = { span: 1, type: "day" } as const;

/** 由查询结果推导一个数据集区块的互斥展示状态。 */
function datasetSectionState(
  isPending: boolean,
  isError: boolean,
  page: MarketDataPage<unknown> | undefined,
): EtfDatasetSectionState {
  if (isPending && page === undefined) return "loading";
  if (isError && page === undefined) return "error";
  if (page === undefined) return "source-unavailable";
  const availability = etfAvailabilityState(page.meta.availability);
  if (availability !== "available") return availability;
  return page.records.length === 0 ? "empty" : "available";
}

/** 根据数据区块状态给最新值卡提供不会误导的回退文案。 */
function latestFallback(state: EtfDatasetSectionState): string {
  const labels: Record<EtfDatasetSectionState, string> = {
    loading: "正在读取",
    error: "读取失败",
    "source-unavailable": "无可读 publication",
    "currently-unsupported": "当前口径不支持",
    empty: "当前窗口无记录",
    available: "来源未披露",
  };

  return labels[state];
}

/** 表示一个独立详情数据集的展示状态与最近一次请求结果。 */
export interface EtfDatasetFailureSignal {
  state: EtfDatasetSectionState;
  isError: boolean;
}

/** 任一初次读取、publication 或保留缓存后的刷新失败都应进入部分失败提示。 */
export function hasPartialDatasetFailure(signals: readonly EtfDatasetFailureSignal[]): boolean {
  return signals.some(
    /** 刷新失败时 state 仍可能是 available，必须独立检查 Query 错误。 */
    ({ state, isError }) => isError || state === "error" || state === "source-unavailable",
  );
}

/** 渲染真实 ETF 产品资料、日线、单位 NAV 与独立状态详情。 */
export function EtfDetailView() {
  const { identity, profile, profileQuery, barsQuery, navsQuery, statesQuery } = useEtfDetail();
  const barsPage = barsQuery.data;
  const navsPage = navsQuery.data;
  const statesPage = statesQuery.data;
  const profileState = datasetSectionState(
    profileQuery.isPending,
    profileQuery.isError,
    profileQuery.data,
  );
  const barState = datasetSectionState(barsQuery.isPending, barsQuery.isError, barsPage);
  const navState = datasetSectionState(navsQuery.isPending, navsQuery.isError, navsPage);
  const stateState = datasetSectionState(statesQuery.isPending, statesQuery.isError, statesPage);
  const profileAvailability =
    profileQuery.data === undefined
      ? undefined
      : etfAvailabilityState(profileQuery.data.meta.availability);
  const bars = useMemo(
    /** 只从已经严格校验的 bar record values 派生日线数组。 */
    () => barsPage?.records.map((record) => record.values) ?? [],
    [barsPage],
  );
  const navs = useMemo(
    /** 只从已经严格校验的 NAV record values 派生单位 NAV 数组。 */
    () => navsPage?.records.map((record) => record.values) ?? [],
    [navsPage],
  );
  const states = useMemo(
    /** 只从已经严格校验的状态 record values 派生状态数组。 */
    () => statesPage?.records.map((record) => record.values) ?? [],
    [statesPage],
  );
  const candles = useMemo(
    /** KLineChart 只接收来源日线转换结果。 */
    () => toEtfCandles(bars),
    [bars],
  );
  const comparisonPoints = useMemo(
    /** ECharts 比较保持价格和 NAV 原值，不计算折溢价。 */
    () => createEtfNavPricePoints(bars, navs),
    [bars, navs],
  );
  const latestBar = latestByDate(bars, (bar) => bar.tradeDate);
  const latestNav = latestByDate(navs, (nav) => nav.navDate);
  const publicationMismatch = publicationDateMismatch([
    { label: "产品资料", meta: profileQuery.data?.meta },
    { label: "日线", meta: barsPage?.meta },
    { label: "单位 NAV", meta: navsPage?.meta },
    { label: "状态", meta: statesPage?.meta },
  ]);
  const hasPartialFailure = hasPartialDatasetFailure([
    { state: profileState, isError: profileQuery.isError },
    { state: barState, isError: barsQuery.isError },
    { state: navState, isError: navsQuery.isError },
    { state: stateState, isError: statesQuery.isError },
  ]);

  /** 零参数重试产品身份查询，避免把 React event 误传给 TanStack Query。 */
  function retryProfile(): void {
    void profileQuery.refetch();
  }

  /** 零参数重试日线查询。 */
  function retryBars(): void {
    void barsQuery.refetch();
  }

  /** 零参数重试单位 NAV 查询。 */
  function retryNavs(): void {
    void navsQuery.refetch();
  }

  /** 零参数重试三维状态查询。 */
  function retryStates(): void {
    void statesQuery.refetch();
  }

  if (identity === null) {
    return (
      <MarketDataStateView
        kind="error"
        title="ETF 路由身份无效"
        description="交易所必须为 SSE 或 SZSE，代码必须为六位数字；页面不会根据代码猜测交易所。"
      />
    );
  }
  if (profileQuery.isPending && profileQuery.data === undefined) {
    return (
      <MarketDataStateView
        kind="loading"
        title="正在读取 ETF 产品身份"
        description="先确认产品目录身份，再并行读取日线、NAV 和独立状态。"
      />
    );
  }
  if (profileQuery.isError && profileQuery.data === undefined) {
    return (
      <MarketDataStateView
        kind="error"
        title="ETF 产品身份请求失败"
        description="产品身份服务暂时不可用。请重试；未经发布的产品资料不会显示。"
        onRetry={retryProfile}
      />
    );
  }
  if (profileAvailability === "source-unavailable") {
    return (
      <MarketDataStateView
        kind="source-unavailable"
        title="ETF 产品目录来源不可用"
        description={`${unavailableReleaseSummary(profileQuery.data.meta)}。无法确认永久产品身份，因此不会发起下游行情查询。`}
        onRetry={retryProfile}
      />
    );
  }
  if (profileAvailability === "currently-unsupported") {
    return (
      <MarketDataStateView
        kind="currently-unsupported"
        title="ETF 产品目录当前不支持"
        description={`${unavailableReleaseSummary(profileQuery.data.meta)}。无法确认永久产品身份，因此不会发起下游行情查询。`}
      />
    );
  }
  if (profileAvailability === "empty") {
    return (
      <MarketDataStateView
        kind="empty"
        title="ETF 产品目录当前没有可公开记录"
        description={`${unavailableReleaseSummary(profileQuery.data.meta)}。目录空结果不代表退市；无法确认产品身份时不会发起下游行情查询。`}
      />
    );
  }
  if (profile === undefined) {
    return (
      <MarketDataStateView
        kind="empty"
        title="产品目录中未找到该 ETF"
        description="目录缺席不代表退市；页面不会用代码前缀或其他证券目录补齐身份。"
      />
    );
  }

  return (
    <Stack spacing={3}>
      <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
        <Box>
          <Button
            component={RouterLink}
            to={`/market/etfs?exchange=${profile.exchange}`}
            color="inherit"
            startIcon={<ArrowBackRoundedIcon />}
            sx={{ mb: 1 }}
          >
            返回 ETF 目录
          </Button>
          <Typography component="h1" variant="h4">
            {profile.displayName}
          </Typography>
          <Typography color="text.secondary" sx={{ mt: 0.5 }}>
            {profile.symbol} · {etfExchangeLabel(profile.exchange)} · Asia/Shanghai
          </Typography>
        </Box>
      </Stack>

      <MarketDataPublication datasetLabel="ETF 产品资料 v2" meta={profileQuery.data.meta} />
      {profileQuery.isError ? (
        <Alert
          severity="warning"
          action={
            <Button color="inherit" size="small" onClick={retryProfile}>
              重试
            </Button>
          }
        >
          产品资料刷新失败，仍展示上一份已校验 publication。
        </Alert>
      ) : null}
      {profileQuery.data.records.length > 1 ? (
        <Alert severity="warning">产品目录返回多个当前身份版本，页面仅使用精确匹配记录。</Alert>
      ) : null}
      {publicationMismatch === null ? null : (
        <Alert severity="warning">{publicationMismatch}</Alert>
      )}
      {hasPartialFailure ? (
        <Alert severity="warning">
          部分数据集暂不可用；各区块独立重试，已成功 publication 继续保留。
        </Alert>
      ) : null}

      <EtfLatestValues
        latestBar={latestBar}
        latestNav={latestNav}
        barFallback={latestFallback(barState)}
        navFallback={latestFallback(navState)}
      />
      <EtfProfileSummary profile={profile} />

      <EtfDatasetSection
        title="日线行情"
        description="KLineChart 展示未复权 OHLC、成交量和成交额；不接入实时增量或本地替代数据。"
        datasetLabel="ETF 未复权日线 v2"
        state={barState}
        meta={barsPage?.meta}
        refreshFailed={barsQuery.isError}
        onRetry={retryBars}
      >
        <Suspense fallback={<Skeleton variant="rounded" height={440} />}>
          <KlinePanel
            symbol={`${profile.exchange}:${profile.symbol}`}
            period={etfDailyPeriod}
            candles={candles}
          />
        </Suspense>
      </EtfDatasetSection>

      <EtfDatasetSection
        title="价格与单位 NAV"
        description="ECharts 以双轴展示价格与单位 NAV 来源原值和各自缺失日，不执行额外派生。"
        datasetLabel="ETF 单位 NAV v2"
        state={navState}
        meta={navsPage?.meta}
        refreshFailed={navsQuery.isError}
        onRetry={retryNavs}
      >
        <Suspense fallback={<Skeleton variant="rounded" height={360} />}>
          <EtfNavPriceChart points={comparisonPoints} />
        </Suspense>
      </EtfDatasetSection>

      <EtfDatasetSection
        title="最近报告的交易、申购与赎回状态"
        description="按最近 365 日各维度选择来源最新报告值与报告日期；缺失交易状态时不会用上市状态补齐，也不声称实时当前状态。"
        datasetLabel="ETF 日级状态 v2"
        state={stateState}
        meta={statesPage?.meta}
        refreshFailed={statesQuery.isError}
        onRetry={retryStates}
      >
        <EtfStatusGrid values={states} />
      </EtfDatasetSection>
    </Stack>
  );
}
