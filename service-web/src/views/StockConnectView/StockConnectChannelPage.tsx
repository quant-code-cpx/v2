import { Alert, Card, CardContent, Chip, Skeleton, Stack, Typography } from "@mui/material";
import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { isApiError } from "../../api/http";
import { ActiveSecurityTable } from "./components/ActiveSecurityTable";
import { ChannelDetailMetrics } from "./components/ChannelDetailMetrics";
import { PublicationBar } from "./components/PublicationBar";
import {
  StockConnectDateFilter,
  StockConnectTrendDaysSelect,
} from "./components/StockConnectFilters";
import { StockConnectPageHeader } from "./components/StockConnectPageHeader";
import { StockConnectReadinessNotice } from "./components/StockConnectReadinessNotice";
import {
  StockConnectErrorState,
  StockConnectPageSkeleton,
} from "./components/StockConnectRemoteState";
import { useStockConnectChannelQueries } from "./hooks/useStockConnectQueries";
import { useStockConnectUrlState } from "./hooks/useStockConnectUrlState";
import {
  stockConnectChannelDescription,
  stockConnectChannelLabel,
} from "./utils/stock-connect-presentation";
import { stockConnectChannelCodeBySlug, stockConnectChannelSlugs } from "./utils/stock-connect-url";
import type {
  StockConnectChannelSlug,
  StockConnectDateUrlValue,
  StockConnectPageSize,
  StockConnectRankingSlug,
  StockConnectTrendDays,
} from "./utils/stock-connect-url";

/** 延迟加载单通道 ECharts 趋势。 */
const StockConnectTrendChart = lazy(async () => {
  const module = await import("./components/StockConnectTrendChart");
  return { default: module.StockConnectTrendChart };
});

/** 把已通过路由 loader 的短名收窄为固定通道；异常时拒绝发送 API。 */
function resolveChannelSlug(value: string | undefined): StockConnectChannelSlug {
  if (!stockConnectChannelSlugs.includes(value as StockConnectChannelSlug)) {
    throw new Response("Not Found", { status: 404 });
  }

  return value as StockConnectChannelSlug;
}

/** 渲染单通道精确交易日统计、日终状态、额度、趋势与来源活跃榜。 */
export function StockConnectChannelPage() {
  const parameters = useParams<{ channel: string }>();
  const channelSlug = resolveChannelSlug(parameters.channel);
  const channel = stockConnectChannelCodeBySlug[channelSlug];
  const { state, update, goToFirstPage, goToNextPage, recoverStaleCursor } =
    useStockConnectUrlState({ fixedChannel: channelSlug });
  const { channelQuery, readinessQuery, activeQuery, parentPublicationRecovery } =
    useStockConnectChannelQueries(channel, state);
  const [cursorRecovered, setCursorRecovered] = useState(false);
  const response = channelQuery.data?.data;
  /** 游标跨 publication 失效时保留全部业务筛选并回到第一页。 */
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

  /** 关闭游标版本恢复提示。 */
  const handleCloseCursorNotice = useCallback(() => {
    setCursorRecovered(false);
  }, []);

  /** 更新精确交易日或 latest，并清除旧游标。 */
  const handleDateChange = useCallback(
    (date: StockConnectDateUrlValue) => {
      update({ date });
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

  /** 重试通道 publication 查询。 */
  const handleRetryChannel = useCallback(() => {
    void channelQuery.refetch();
  }, [channelQuery]);

  /** 只刷新该通道的 readiness 证据，不改变详情 publication。 */
  const handleRetryReadiness = useCallback(() => {
    void readinessQuery.refetch();
  }, [readinessQuery]);

  /** 普通错误只重试榜单；父版本失配必须整体复核通道 publication 与榜单。 */
  const handleRetryActive = useCallback(() => {
    if (isApiError(activeQuery.error) && activeQuery.error.code === "PARENT_PUBLICATION_MISMATCH") {
      parentPublicationRecovery.retryPublicationPair();
      return;
    }
    void activeQuery.refetch();
  }, [activeQuery, parentPublicationRecovery]);

  /** 从精确日缺失状态恢复到该通道 latest。 */
  const handleReturnLatest = useCallback(() => {
    update({ date: "latest" });
  }, [update]);

  return (
    <Stack spacing={3}>
      <StockConnectPageHeader
        eyebrow={`${stockConnectChannelDescription(channel)} · ${channel}`}
        title={stockConnectChannelLabel(channel)}
        description="精确交易日与日终状态由正式 publication 返回。成交金额逐字段标注 CNY/HKD；额度金额恒为 CNY；不同币种不换算、不合计。"
        breadcrumb={stockConnectChannelLabel(channel)}
        actions={
          <Stack direction="row" spacing={1}>
            <Chip color="info" label="日终通道状态" />
            <Chip color="success" label="来源可追溯" />
          </Stack>
        }
      />
      <Stack direction="row" spacing={1.5}>
        <StockConnectDateFilter value={state.date} onChange={handleDateChange} />
        <StockConnectTrendDaysSelect value={state.trendDays} onChange={handleTrendDaysChange} />
      </Stack>
      <StockConnectReadinessNotice
        readiness={readinessQuery.data?.data}
        isPending={readinessQuery.isPending}
        error={readinessQuery.error}
        onRetry={handleRetryReadiness}
      />

      {cursorRecovered ? (
        <Alert severity="info" onClose={handleCloseCursorNotice}>
          榜单 publication 已更新，旧游标已清除；日期、通道和榜单筛选均已保留。
        </Alert>
      ) : null}
      {parentPublicationRecovery.status === "recovering" ? (
        <Alert severity="info">
          父 publication 已更新，正在整体复核通道详情与来源活跃榜；复核完成前不会拼接两个版本。
        </Alert>
      ) : parentPublicationRecovery.status === "exhausted" ? (
        <Alert severity="warning">
          publication 连续更新，已停止自动复核。当前榜单未与通道详情拼接；请在榜单区手动整体重试。
        </Alert>
      ) : null}

      {response === undefined && channelQuery.isPending ? (
        <StockConnectPageSkeleton />
      ) : response === undefined && channelQuery.isError ? (
        <StockConnectErrorState
          error={channelQuery.error}
          onRetry={handleRetryChannel}
          onLatest={handleReturnLatest}
          dateSelection={state.date}
        />
      ) : response !== undefined ? (
        <>
          <PublicationBar
            publication={response.publication}
            resolvedTradeDate={response.resolvedTradeDate}
            resolutionLabel={
              response.dateResolution === "LATEST_CHANNEL" ? "通道 latest 交易日" : "精确交易日"
            }
            isFetching={channelQuery.isFetching}
            isStaleBecauseError={channelQuery.isError}
          />
          <ChannelDetailMetrics summary={response.channel} />
          <Card>
            <CardContent>
              <Stack spacing={1.5}>
                <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
                  <Stack>
                    <Typography variant="h5">通道交易日趋势</Typography>
                    <Typography variant="body2" color="text.secondary">
                      休市日断点保留；成交额与可用净额按字段原币绘制，出现混合币种时停止绘图。
                    </Typography>
                  </Stack>
                  <Chip size="small" color="info" label={`${state.trendDays} 个交易日`} />
                </Stack>
                <Suspense fallback={<Skeleton variant="rounded" height={320} />}>
                  <StockConnectTrendChart channel={channel} points={response.trend} />
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
