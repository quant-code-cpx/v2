import { RefreshRounded as RefreshRoundedIcon } from "@mui/icons-material";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  LinearProgress,
  Stack,
  Typography,
} from "@mui/material";
import { Link as RouterLink } from "react-router-dom";

import { MarketDataPublication } from "../../components/MarketDataPublication";
import { MarketDataStateView } from "../../components/MarketDataStateView";
import { EtfFilters } from "./components/EtfFilters";
import { EtfTable } from "./components/EtfTable";
import { useEtfList } from "./hooks/useEtfList";
import type { EtfListFilters } from "../../types/etf";
import { etfAvailabilityState, unavailableReleaseSummary } from "../../utils/etf-presentation";

/** 渲染 URL 可分享、cursor 分页且只消费真实 v2 publication 的 ETF 目录。 */
export function EtfListView() {
  const {
    filters,
    query,
    applyFilters,
    goToNextPage,
    restartPagination,
    resetFilters,
    cursorRecoveryNotice,
    dismissCursorRecoveryNotice,
  } = useEtfList();
  const page = query.data;
  const availability =
    page === undefined ? undefined : etfAvailabilityState(page.meta.availability);

  /** 只在 typed reader 允许的两个字段间切换真实排序。 */
  function handleSortChange(field: EtfListFilters["sort"], order: EtfListFilters["order"]): void {
    applyFilters({ sort: field, order });
  }

  /** 零参数重试目录查询，避免把 React event 当成 TanStack refetch options。 */
  function retryList(): void {
    void query.refetch();
  }

  return (
    <Stack spacing={3}>
      <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
        <Box>
          <Typography component="h1" variant="h4">
            ETF 产品目录
          </Typography>
          <Typography color="text.secondary" sx={{ mt: 0.5 }}>
            独立于股票中心；当前目录按交易所和代码/名称查询，生命周期状态未可靠披露前不提供状态筛选。
          </Typography>
        </Box>
        <Stack direction="row" spacing={1}>
          <Button component={RouterLink} to="/market/funds" color="inherit">
            返回基金入口
          </Button>
          <Button
            variant="outlined"
            startIcon={<RefreshRoundedIcon />}
            onClick={retryList}
            disabled={query.isFetching}
          >
            刷新目录
          </Button>
        </Stack>
      </Stack>

      <EtfFilters filters={filters} onApply={applyFilters} onReset={resetFilters} />

      {cursorRecoveryNotice ? (
        <Alert severity="info" onClose={dismissCursorRecoveryNotice}>
          目录已更新，已返回第一页并保留筛选条件
        </Alert>
      ) : null}

      <Card>
        {query.isFetching ? <LinearProgress aria-label="正在更新 ETF 目录" /> : null}
        <CardContent sx={{ p: 3 }}>
          {query.isPending && page === undefined ? (
            <MarketDataStateView
              kind="loading"
              title="正在读取 ETF 产品目录"
              description="等待可公开读取的产品目录 publication。"
            />
          ) : null}
          {query.isError && page === undefined ? (
            <MarketDataStateView
              kind="error"
              title="ETF 产品目录请求失败"
              description="产品身份或数据服务暂时不可用。请重试；未经发布的数据不会显示。"
              onRetry={retryList}
            />
          ) : null}
          {page !== undefined && availability === "source-unavailable" ? (
            <MarketDataStateView
              kind="source-unavailable"
              title="ETF 产品目录来源不可用"
              description={`${unavailableReleaseSummary(page.meta)}。页面不会根据代码特征补齐产品目录。`}
              onRetry={retryList}
            />
          ) : null}
          {page !== undefined && availability === "currently-unsupported" ? (
            <MarketDataStateView
              kind="currently-unsupported"
              title="ETF 产品目录当前不支持"
              description={`${unavailableReleaseSummary(page.meta)}。当前来源口径不能安全映射到冻结合同。`}
            />
          ) : null}
          {page !== undefined && availability === "empty" ? (
            <MarketDataStateView
              kind="empty"
              title="ETF 产品目录当前没有可公开记录"
              description={`${unavailableReleaseSummary(page.meta)}。目录空结果不代表产品退市，页面也不会使用代码前缀补齐。`}
            />
          ) : null}
          {page !== undefined && availability === "available" ? (
            <Stack spacing={2.5}>
              {query.isError ? (
                <Alert severity="warning">
                  刷新失败，当前仍展示上一份已校验 publication。可稍后重试。
                </Alert>
              ) : null}
              <MarketDataPublication datasetLabel="ETF 产品目录 v2" meta={page.meta} />
              {page.records.length === 0 ? (
                <MarketDataStateView
                  kind="empty"
                  title="没有匹配的 ETF"
                  description="当前筛选下没有公开记录；这不代表对应产品已经退市。"
                />
              ) : (
                <EtfTable
                  page={page}
                  filters={filters}
                  isUpdating={query.isPlaceholderData || query.isFetching}
                  onSortChange={handleSortChange}
                  onNextPage={goToNextPage}
                  onRestart={restartPagination}
                />
              )}
            </Stack>
          ) : null}
        </CardContent>
      </Card>
    </Stack>
  );
}
