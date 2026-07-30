import {
  Alert,
  Box,
  Button,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Skeleton,
  Stack,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tabs,
  Typography,
} from "@mui/material";
import { useCallback } from "react";
import type { SyntheticEvent } from "react";
import type { SelectChangeEvent } from "@mui/material";

import type {
  StockConnectActiveSecurityPage,
  VersionedStockConnectResponse,
} from "../../../types/stock-connect";
import {
  stockConnectAvailabilityLabel,
  stockConnectRankingLabel,
} from "../utils/stock-connect-presentation";
import { stockConnectPageSizeOptions, stockConnectRankingBySlug } from "../utils/stock-connect-url";
import type { StockConnectPageSize, StockConnectRankingSlug } from "../utils/stock-connect-url";
import { ActiveSecurityTableRow } from "./ActiveSecurityTableRow";
import { StockConnectEmptyState, StockConnectErrorState } from "./StockConnectRemoteState";

/** 描述来源活跃证券查询在表格需要的远程状态。 */
interface ActiveSecurityQueryState {
  data?: VersionedStockConnectResponse<StockConnectActiveSecurityPage>;
  error: unknown;
  isPending: boolean;
  isError: boolean;
  isFetching: boolean;
}

/** 描述官方来源活跃榜的 URL 控件、分页动作和远程数据。 */
interface ActiveSecurityTableProps {
  query: ActiveSecurityQueryState;
  ranking: StockConnectRankingSlug;
  pageSize: StockConnectPageSize;
  hasCursor: boolean;
  onRankingChange: (ranking: StockConnectRankingSlug) => void;
  onPageSizeChange: (pageSize: StockConnectPageSize) => void;
  onFirstPage: () => void;
  onNextPage: (cursor: string) => void;
  onRetry: () => void;
}

/** 判断当前来源榜记录是否包含可用于榜内净额排序的真实字段。 */
function hasAvailableNetAmount(page: StockConnectActiveSecurityPage): boolean {
  return page.items.some(
    /** 只检查字段 availability，不根据成交额或金额大小推断。 */
    (item) => item.netBuyAmount.availability === "DERIVED",
  );
}

/** 渲染来源活跃证券榜、制度未披露状态和游标分页，绝不称为全市场排行。 */
export function ActiveSecurityTable({
  query,
  ranking,
  pageSize,
  hasCursor,
  onRankingChange,
  onPageSizeChange,
  onFirstPage,
  onNextPage,
  onRetry,
}: ActiveSecurityTableProps) {
  const page = query.data?.data;
  const currentRankingUnavailable =
    page !== undefined &&
    ((page.ranking === "SOURCE_ACTIVE" && page.rankingAvailability !== "REPORTED") ||
      (page.ranking !== "SOURCE_ACTIVE" && page.rankingAvailability !== "DERIVED"));
  const netTabsDisabled =
    page !== undefined &&
    (currentRankingUnavailable ||
      (page.ranking === "SOURCE_ACTIVE" && page.items.length > 0 && !hasAvailableNetAmount(page)));
  /** 为每一种净额 Tab 禁用原因生成邻接且可聚焦的明确说明。 */
  const netTabsDisabledReason =
    page === undefined || !netTabsDisabled
      ? null
      : currentRankingUnavailable
        ? `${stockConnectRankingLabel(stockConnectRankingBySlug[ranking])}当前不可展示（合同状态：${stockConnectAvailabilityLabel(page.rankingAvailability)}）。不会回退为成交活跃榜，也不会从成交额推导净买入或净卖出。`
        : "当前来源活跃证券记录未同时报告买入和卖出，无法生成榜内净额排序。净额 Tab 已禁用；不会从成交额推导净买入或净卖出。";

  /** 将榜单 Tab 更新为 URL 稳定短名。 */
  const handleRankingChange = useCallback(
    (_event: SyntheticEvent, value: StockConnectRankingSlug) => {
      onRankingChange(value);
    },
    [onRankingChange],
  );

  /** 将分页大小限制为冻结的 20、50 或 100。 */
  const handlePageSizeChange = useCallback(
    (event: SelectChangeEvent<string>) => {
      onPageSizeChange(Number(event.target.value) as StockConnectPageSize);
    },
    [onPageSizeChange],
  );

  /** 使用服务端不透明游标进入下一页。 */
  const handleNextPage = useCallback(() => {
    if (page?.nextCursor !== null && page?.nextCursor !== undefined) {
      onNextPage(page.nextCursor);
    }
  }, [onNextPage, page?.nextCursor]);

  return (
    <Stack spacing={2}>
      <Stack direction="row" justifyContent="space-between" alignItems="center" spacing={2}>
        <Box>
          <Typography variant="h5">来源活跃证券</Typography>
          <Typography variant="body2" color="text.secondary">
            官方 Top 10 · 非全市场排行 · 净额排序仅限来源榜内
          </Typography>
        </Box>
        <FormControl sx={{ width: 132 }}>
          <InputLabel id="stock-connect-page-size-label">每页</InputLabel>
          <Select
            labelId="stock-connect-page-size-label"
            label="每页"
            value={String(pageSize)}
            onChange={handlePageSizeChange}
          >
            {stockConnectPageSizeOptions.map(
              /** 渲染公开合同允许的分页大小。 */
              (size) => (
                <MenuItem key={size} value={String(size)}>
                  {size} 条
                </MenuItem>
              ),
            )}
          </Select>
        </FormControl>
      </Stack>

      <Tabs value={ranking} onChange={handleRankingChange} aria-label="来源活跃证券榜单口径">
        <Tab value="active" label="活跃证券" />
        <Tab
          value="net-buy"
          label="榜内净买入"
          disabled={netTabsDisabled}
          aria-describedby={netTabsDisabled ? "stock-connect-ranking-notice" : undefined}
        />
        <Tab
          value="net-sell"
          label="榜内净卖出"
          disabled={netTabsDisabled}
          aria-describedby={netTabsDisabled ? "stock-connect-ranking-notice" : undefined}
        />
      </Tabs>

      {netTabsDisabledReason !== null ? (
        <Alert id="stock-connect-ranking-notice" severity="info" tabIndex={0}>
          {netTabsDisabledReason}
        </Alert>
      ) : null}
      {query.isError && page !== undefined ? (
        <Alert severity="warning">
          榜单复核失败，当前保留 dataVersion {query.data?.dataVersion}
        </Alert>
      ) : null}

      {query.isPending ? (
        <Stack spacing={1} aria-busy="true" aria-label="正在加载来源活跃证券">
          {Array.from(
            { length: 5 },
            /** 保留榜单首屏行高，避免加载完成时布局跳动。 */
            (_, index) => (
              <Skeleton key={index} variant="rounded" height={52} />
            ),
          )}
        </Stack>
      ) : query.isError && page === undefined ? (
        <StockConnectErrorState error={query.error} onRetry={onRetry} />
      ) : page !== undefined && currentRankingUnavailable ? (
        <StockConnectEmptyState
          title={`${stockConnectRankingLabel(page.ranking)}不可用`}
          description={`${stockConnectAvailabilityLabel(page.rankingAvailability)}。接口未返回记录和游标，页面不会回退为成交活跃榜，也不会从成交额推导净额。`}
        />
      ) : page !== undefined && page.items.length === 0 ? (
        <StockConnectEmptyState
          title={hasCursor ? "当前游标页没有记录" : "该日来源活跃榜为空"}
          description={
            hasCursor
              ? "服务端未返回当前页记录；可回到第一页，不代表全市场无成交。"
              : "官方来源在所选通道与交易日没有返回活跃证券记录，不代表全市场无成交。"
          }
          actionLabel={hasCursor ? "返回第一页" : undefined}
          onAction={hasCursor ? onFirstPage : undefined}
        />
      ) : page !== undefined ? (
        <>
          <TableContainer sx={{ overflowX: "auto" }}>
            <Table
              size="small"
              aria-label={`${stockConnectRankingLabel(page.ranking)}，仅限来源活跃证券`}
              sx={{ minWidth: 920 }}
            >
              <TableHead>
                <TableRow>
                  <TableCell scope="col">当前榜单名次</TableCell>
                  <TableCell scope="col">来源活跃名次</TableCell>
                  <TableCell scope="col">证券身份</TableCell>
                  <TableCell scope="col" align="right">
                    买入
                  </TableCell>
                  <TableCell scope="col" align="right">
                    卖出
                  </TableCell>
                  <TableCell scope="col" align="right">
                    成交额
                  </TableCell>
                  <TableCell scope="col" align="right">
                    净额
                  </TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {page.items.map(
                  /** 使用稳定证券身份或来源代码与名次渲染官方记录。 */
                  (item) => (
                    <ActiveSecurityTableRow
                      key={
                        item.identity.instrumentEntityRef ??
                        `${item.identity.sourceSecurityCode}-${item.rankingRank}`
                      }
                      item={item}
                      channel={page.channel}
                      resolvedTradeDate={page.resolvedTradeDate}
                    />
                  ),
                )}
              </TableBody>
            </Table>
          </TableContainer>
          <Stack direction="row" justifyContent="flex-end" spacing={1.5}>
            <Button variant="outlined" disabled={!hasCursor} onClick={onFirstPage}>
              返回第一页
            </Button>
            <Button
              variant="contained"
              disabled={page.nextCursor === null || query.isFetching}
              onClick={handleNextPage}
            >
              下一页
            </Button>
          </Stack>
        </>
      ) : null}
    </Stack>
  );
}
