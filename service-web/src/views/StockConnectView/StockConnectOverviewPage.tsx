import { Alert, Card, CardContent, Chip, Skeleton, Stack, Typography } from "@mui/material";
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useLocation } from "react-router-dom";

import { isApiError } from "../../api/http";
import { ActiveSecurityTable } from "./components/ActiveSecurityTable";
import { ChannelSummaryCard } from "./components/ChannelSummaryCard";
import { PublicationBar } from "./components/PublicationBar";
import { StockConnectOverviewToolbar } from "./components/StockConnectOverviewToolbar";
import { StockConnectPageHeader } from "./components/StockConnectPageHeader";
import { StockConnectReadinessNotice } from "./components/StockConnectReadinessNotice";
import {
  StockConnectErrorState,
  StockConnectPageSkeleton,
} from "./components/StockConnectRemoteState";
import { useStockConnectOverviewQueries } from "./hooks/useStockConnectQueries";
import { useStockConnectUrlState } from "./hooks/useStockConnectUrlState";
import {
  stockConnectChannelCodeBySlug,
  stockConnectChannelSlugByCode,
  stockConnectChannelsForDirection,
} from "./utils/stock-connect-url";
import type {
  StockConnectChannelSlug,
  StockConnectDateUrlValue,
  StockConnectDirectionFilter,
  StockConnectPageSize,
  StockConnectRankingSlug,
  StockConnectTrendDays,
} from "./utils/stock-connect-url";

/** 延迟加载 ECharts 趋势组件，首屏路由先完成标题、筛选和 publication。 */
const StockConnectTrendChart = lazy(async () => {
  const module = await import("./components/StockConnectTrendChart");
  return { default: module.StockConnectTrendChart };
});

/** 渲染四通道共同交易日总览、单通道趋势和来源活跃证券榜。 */
export function StockConnectOverviewPage() {
  const location = useLocation();
  const { state, update, goToFirstPage, goToNextPage, recoverStaleCursor } =
    useStockConnectUrlState();
  const { overviewQuery, readinessQuery, activeQuery, selectedChannel, parentPublicationRecovery } =
    useStockConnectOverviewQueries(state);
  const [cursorRecovered, setCursorRecovered] = useState(false);
  const overview = overviewQuery.data?.data;

  /** 构造进入通道详情时应保留的日期、榜单和窗口筛选。 */
  const detailSearch = useMemo(() => {
    const parameters = new URLSearchParams(location.search);
    parameters.delete("direction");
    parameters.delete("channel");
    parameters.delete("cursor");
    const serialized = parameters.toString();
    return serialized.length === 0 ? "" : `?${serialized}`;
  }, [location.search]);

  /** 游标不属于当前 publication 时保留业务筛选并自动回到第一页。 */
  useEffect(() => {
    if (
      state.cursor !== undefined &&
      isApiError(activeQuery.error) &&
      activeQuery.error.code === "CURSOR_VERSION_MISMATCH"
    ) {
      setCursorRecovered(true);
      recoverStaleCursor();
    }
  }, [activeQuery.error, recoverStaleCursor, state.cursor]);

  /** 手动关闭游标恢复提示。 */
  const handleCloseCursorNotice = useCallback(() => {
    setCursorRecovered(false);
  }, []);

  /** 更新交易日并清除旧 publication 游标。 */
  const handleDateChange = useCallback(
    (date: StockConnectDateUrlValue) => {
      update({ date });
    },
    [update],
  );

  /** 更新方向并保证趋势通道属于该方向。 */
  const handleDirectionChange = useCallback(
    (direction: StockConnectDirectionFilter) => {
      const channelCodes = stockConnectChannelsForDirection(direction);
      const currentChannel = stockConnectChannelCodeBySlug[state.channel];
      const nextChannel = channelCodes.includes(currentChannel)
        ? state.channel
        : stockConnectChannelSlugByCode[channelCodes[0] ?? "SH_NORTHBOUND"];
      update({ direction, channel: nextChannel });
    },
    [state.channel, update],
  );

  /** 更新趋势与来源榜通道，不改变方向集合。 */
  const handleChannelChange = useCallback(
    (channel: StockConnectChannelSlug) => {
      update({ channel });
    },
    [update],
  );

  /** 更新交易日趋势窗口。 */
  const handleTrendDaysChange = useCallback(
    (trendDays: StockConnectTrendDays) => {
      update({ trendDays });
    },
    [update],
  );

  /** 更新来源榜排序并回到第一页。 */
  const handleRankingChange = useCallback(
    (ranking: StockConnectRankingSlug) => {
      update({ ranking });
    },
    [update],
  );

  /** 更新来源榜分页大小并回到第一页。 */
  const handlePageSizeChange = useCallback(
    (pageSize: StockConnectPageSize) => {
      update({ pageSize });
    },
    [update],
  );

  /** 重试总览 publication 读取。 */
  const handleRetryOverview = useCallback(() => {
    void overviewQuery.refetch();
  }, [overviewQuery]);

  /** 只刷新独立 readiness 证据，不使业务 publication 与候选状态相互污染。 */
  const handleRetryReadiness = useCallback(() => {
    void readinessQuery.refetch();
  }, [readinessQuery]);

  /** 普通错误只重试榜单；父版本失配必须整体复核父 publication 与榜单。 */
  const handleRetryActive = useCallback(() => {
    if (isApiError(activeQuery.error) && activeQuery.error.code === "PARENT_PUBLICATION_MISMATCH") {
      parentPublicationRecovery.retryPublicationPair();
      return;
    }
    void activeQuery.refetch();
  }, [activeQuery, parentPublicationRecovery]);

  /** 从精确日错误恢复为 latest，但不伪装成同一请求结果。 */
  const handleReturnLatest = useCallback(() => {
    update({ date: "latest" });
  }, [update]);

  return (
    <Stack spacing={3}>
      <StockConnectPageHeader
        eyebrow="市场数据 / 互联互通"
        title="沪深港通与跨境互联互通"
        description="四条通道日终成交、状态、额度与来源活跃证券。CNY、HKD 分开呈现，不把成交额标作资金净流入。"
        actions={
          <Stack direction="row" spacing={1}>
            <Chip color="info" label="日终 publication" />
            <Chip color="success" label="官方来源链" />
          </Stack>
        }
      />
      <StockConnectOverviewToolbar
        date={state.date}
        direction={state.direction}
        channel={state.channel}
        trendDays={state.trendDays}
        onDateChange={handleDateChange}
        onDirectionChange={handleDirectionChange}
        onChannelChange={handleChannelChange}
        onTrendDaysChange={handleTrendDaysChange}
      />
      <StockConnectReadinessNotice
        readiness={readinessQuery.data?.data}
        isPending={readinessQuery.isPending}
        error={readinessQuery.error}
        onRetry={handleRetryReadiness}
      />

      {cursorRecovered ? (
        <Alert severity="info" onClose={handleCloseCursorNotice}>
          榜单 publication 已更新，旧游标已清除；日期、方向、通道和榜单筛选均已保留。
        </Alert>
      ) : null}
      {parentPublicationRecovery.status === "recovering" ? (
        <Alert severity="info">
          父 publication
          已更新，正在整体复核共同交易日总览与来源活跃榜；复核完成前不会拼接两个版本。
        </Alert>
      ) : parentPublicationRecovery.status === "exhausted" ? (
        <Alert severity="warning">
          publication 连续更新，已停止自动复核。当前榜单未与总览拼接；请在榜单区手动整体重试。
        </Alert>
      ) : null}

      {overview === undefined && overviewQuery.isPending ? (
        <StockConnectPageSkeleton />
      ) : overview === undefined && overviewQuery.isError ? (
        <StockConnectErrorState
          error={overviewQuery.error}
          onRetry={handleRetryOverview}
          onLatest={handleReturnLatest}
          dateSelection={state.date}
        />
      ) : overview !== undefined ? (
        <>
          <PublicationBar
            publication={overview.publication}
            resolvedTradeDate={overview.resolvedTradeDate}
            resolutionLabel={
              overview.dateResolution === "LATEST_COMMON" ? "共同交易日" : "精确交易日"
            }
            isFetching={overviewQuery.isFetching}
            isStaleBecauseError={overviewQuery.isError}
          />
          <Stack direction="row" spacing={1.5} sx={{ color: "text.secondary" }}>
            <Chip size="small" label={`${overview.channels.length} 条通道逐项呈现`} />
            <Chip size="small" label="不同币种不合计" />
          </Stack>
          <Stack
            sx={{
              display: "grid",
              gridTemplateColumns: `repeat(${overview.channels.length}, minmax(0, 1fr))`,
              gap: 2,
            }}
          >
            {overview.channels.map(
              /** 每条通道独立渲染，禁止在卡片层求和。 */
              (summary) => (
                <ChannelSummaryCard key={summary.channel} summary={summary} search={detailSearch} />
              ),
            )}
          </Stack>
          <Card>
            <CardContent>
              <Stack spacing={1.5}>
                <Stack>
                  <Typography variant="h5">通道成交额趋势</Typography>
                  <Typography variant="body2" color="text.secondary">
                    按 URL 选中通道绘制日终成交额与可用净额；不跨通道、币种聚合，不补休市断点。
                  </Typography>
                </Stack>
                <Suspense fallback={<Skeleton variant="rounded" height={320} />}>
                  <StockConnectTrendChart channel={selectedChannel} points={overview.trend} />
                </Suspense>
              </Stack>
            </CardContent>
          </Card>
          <Card>
            <CardContent>
              <ActiveSecurityTable
                query={activeQuery}
                ranking={state.ranking}
                pageSize={state.pageSize}
                hasCursor={state.cursor !== undefined}
                onRankingChange={handleRankingChange}
                onPageSizeChange={handlePageSizeChange}
                onFirstPage={goToFirstPage}
                onNextPage={goToNextPage}
                onRetry={handleRetryActive}
              />
            </CardContent>
          </Card>
        </>
      ) : null}
    </Stack>
  );
}
