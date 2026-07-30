import type { ChangeEvent } from "react";
import {
  Alert,
  Box,
  Card,
  CardContent,
  Chip,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Skeleton,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from "@mui/material";
import type { SelectChangeEvent } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { Link as RouterLink, useParams, useSearchParams } from "react-router-dom";

import {
  marketSectorBarsQueryOptions,
  marketSectorConstituentsQueryOptions,
  marketSectorSnapshotQueryOptions,
} from "../../api/market";
import { MarketDataState } from "../../components/MarketDataState";
import { MarketDirectionalValue } from "../../components/MarketDirectionalValue";
import { MarketKLineChart } from "../../components/MarketKLineChart";
import { MarketPageHeader } from "../../components/MarketPageHeader";
import { marketSectorSchemes } from "../../types/market";
import type { MarketSectorScheme } from "../../types/market";
import {
  formatCnyYi,
  formatMarketDateTime,
  formatSourceDecimal,
} from "../../utils/market-formatters";

/** 返回上海时区今日日期，用于仅限定默认查询窗口。 */
function shanghaiToday(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

/** 将查询结束日期向前平移自然日。 */
function subtractCalendarDays(date: string, days: number): string {
  const value = new Date(`${date}T00:00:00Z`);
  value.setUTCDate(value.getUTCDate() - days);
  return value.toISOString().slice(0, 10);
}

/** 把上市状态转换为不会混淆普通停牌与暂停上市的页面文案。 */
function listingStatusLabel(status: "LISTED" | "SUSPENDED" | "DELISTED"): string {
  if (status === "LISTED") return "上市";
  if (status === "SUSPENDED") return "暂停上市";
  return "退市";
}

/** 渲染单个东财板块的快照、原生周期 K 线和当前观察成分。 */
export function MarketSectorDetailView() {
  const { scheme: rawScheme = "", sectorCode: rawSectorCode = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const scheme = marketSectorSchemes.includes(rawScheme as MarketSectorScheme)
    ? (rawScheme as MarketSectorScheme)
    : undefined;
  const sectorCode = rawSectorCode.trim();
  const period =
    searchParams.get("period") === "1w" || searchParams.get("period") === "1mo"
      ? (searchParams.get("period") as "1w" | "1mo")
      : "1d";
  const asOf = searchParams.get("asOf") ?? undefined;
  const defaultEnd = asOf ?? shanghaiToday();
  const end = searchParams.get("end") ?? defaultEnd;
  const start = searchParams.get("start") ?? subtractCalendarDays(end, 370);
  const canQuery = scheme !== undefined && sectorCode.length > 0;

  const snapshotQuery = useQuery({
    ...marketSectorSnapshotQueryOptions(
      scheme ?? "eastmoney.industry",
      sectorCode || "invalid",
      asOf,
    ),
    enabled: canQuery,
  });
  const barsQuery = useQuery({
    ...marketSectorBarsQueryOptions({
      scheme: scheme ?? "eastmoney.industry",
      sectorCode: sectorCode || "invalid",
      period,
      start,
      end,
      limit: 500,
    }),
    enabled: canQuery,
  });
  const constituentsQuery = useQuery({
    ...marketSectorConstituentsQueryOptions(
      scheme ?? "eastmoney.industry",
      sectorCode || "invalid",
      asOf,
    ),
    enabled: canQuery,
  });

  /** 更新图表 URL 状态；查询口径可以复制、刷新和前进后退。 */
  function updateChartUrl(key: "period" | "start" | "end", value: string): void {
    const next = new URLSearchParams(searchParams);
    next.set(key, value);
    setSearchParams(next);
  }

  /** 切换供应商原生日、周或月物理周期。 */
  function handlePeriodChange(event: SelectChangeEvent): void {
    updateChartUrl("period", event.target.value);
  }

  /** 更新 K 线起始日。 */
  function handleStartChange(event: ChangeEvent<HTMLInputElement>): void {
    updateChartUrl("start", event.target.value);
  }

  /** 更新 K 线结束日。 */
  function handleEndChange(event: ChangeEvent<HTMLInputElement>): void {
    updateChartUrl("end", event.target.value);
  }

  /** 并行条件刷新快照、K 线和成分三个独立 publication。 */
  function handleRefresh(): void {
    void Promise.all([snapshotQuery.refetch(), barsQuery.refetch(), constituentsQuery.refetch()]);
  }

  if (!canQuery) {
    return (
      <Stack spacing={3}>
        <MarketPageHeader title="板块详情" subtitle="路由必须包含受支持体系和板块代码。" />
        <MarketDataState
          variant="error"
          title="板块路由无效"
          message="支持 eastmoney.industry 与 eastmoney.concept，且 sectorCode 不能为空。"
          minHeight={360}
        />
      </Stack>
    );
  }

  const snapshot = snapshotQuery.data?.payload;
  const title = snapshot?.name ?? sectorCode;

  return (
    <Stack spacing={3}>
      <MarketPageHeader
        title={title}
        subtitle={`${scheme === "eastmoney.industry" ? "东财行业" : "东财概念"} · ${sectorCode}；不映射为申万同名行业。`}
        status={
          snapshot === undefined ? (
            <Chip size="small" variant="outlined" label="等待 EOD 快照" />
          ) : (
            <Chip
              size="small"
              color={snapshot.qualityStatus === "passed" ? "success" : "warning"}
              variant="outlined"
              label={`${snapshot.tradeDate} · 输入版本 ${snapshot.inputDataVersions.length} 个 · ${snapshot.qualityStatus === "passed" ? "质量通过" : "质量提醒"}`}
            />
          )
        }
        onRefresh={handleRefresh}
        refreshing={
          snapshotQuery.isFetching || barsQuery.isFetching || constituentsQuery.isFetching
        }
      />
      {snapshotQuery.isError ? (
        <Alert severity="warning">
          EOD 快照读取失败；K 线和成分 publication 仍按独立失败边界继续请求。
        </Alert>
      ) : null}
      {snapshot === undefined ? (
        snapshotQuery.isPending ? (
          <Skeleton variant="rounded" height={156} />
        ) : (
          <MarketDataState
            variant="error"
            title="板块快照不可用"
            message="未返回真实 EOD 快照，页面不会从 K 线尾部或目录字段补出摘要。"
            onRetry={
              /** 仅重试当前板块快照。 */
              () => void snapshotQuery.refetch()
            }
          />
        )
      ) : (
        <Box
          component="section"
          aria-label="板块快照"
          sx={{ display: "grid", gridTemplateColumns: "repeat(5, minmax(0, 1fr))", gap: 2 }}
        >
          <Card>
            <CardContent>
              <Typography variant="caption" color="text.secondary">
                板块点位
              </Typography>
              <Typography variant="h5" sx={{ mt: 1 }}>
                {formatSourceDecimal(snapshot.latestValue)}
              </Typography>
            </CardContent>
          </Card>
          <Card>
            <CardContent>
              <Typography variant="caption" color="text.secondary">
                涨跌幅
              </Typography>
              <Box sx={{ mt: 1 }}>
                <MarketDirectionalValue value={snapshot.changePercent} />
              </Box>
            </CardContent>
          </Card>
          <Card>
            <CardContent>
              <Typography variant="caption" color="text.secondary">
                换手率
              </Typography>
              <Typography variant="h6" sx={{ mt: 1 }}>
                {formatSourceDecimal(snapshot.turnoverPercent, "%")}
              </Typography>
            </CardContent>
          </Card>
          <Card>
            <CardContent>
              <Typography variant="caption" color="text.secondary">
                市值
              </Typography>
              <Typography variant="h6" sx={{ mt: 1 }}>
                {snapshot.marketValue === null ? "来源未报告" : formatCnyYi(snapshot.marketValue)}
              </Typography>
            </CardContent>
          </Card>
          <Card>
            <CardContent>
              <Typography variant="caption" color="text.secondary">
                上涨 / 下跌成分
              </Typography>
              <Typography variant="h6" sx={{ mt: 1 }}>
                {snapshot.advancers === null || snapshot.decliners === null
                  ? "来源未报告"
                  : `${snapshot.advancers} / ${snapshot.decliners}`}
              </Typography>
            </CardContent>
          </Card>
        </Box>
      )}
      <Card component="section" aria-label="板块 K 线">
        <CardContent>
          <Stack direction="row" spacing={2} alignItems="center">
            <Typography variant="h6">板块 K 线</Typography>
            <FormControl size="small" sx={{ minWidth: 104, ml: "auto" }}>
              <InputLabel id="sector-period-label">周期</InputLabel>
              <Select
                labelId="sector-period-label"
                value={period}
                label="周期"
                onChange={handlePeriodChange}
              >
                <MenuItem value="1d">日 K</MenuItem>
                <MenuItem value="1w">周 K</MenuItem>
                <MenuItem value="1mo">月 K</MenuItem>
              </Select>
            </FormControl>
            <TextField
              size="small"
              type="date"
              label="起始日"
              value={start}
              onChange={handleStartChange}
              slotProps={{ inputLabel: { shrink: true } }}
            />
            <TextField
              size="small"
              type="date"
              label="结束日"
              value={end}
              onChange={handleEndChange}
              slotProps={{ inputLabel: { shrink: true } }}
            />
          </Stack>
          <Typography variant="caption" color="text.secondary">
            日、周、月均读取供应商独立物理周期；浏览器不聚合日线。
          </Typography>
          {barsQuery.isPending ? (
            <Skeleton variant="rounded" height={430} sx={{ mt: 2 }} />
          ) : barsQuery.data === undefined ? (
            <MarketDataState
              variant="error"
              title="板块 K 线不可用"
              message="真实周期 publication 未载入；不会用其他周期或 EOD 单点代替。"
              onRetry={
                /** 仅重试当前周期 K 线。 */
                () => void barsQuery.refetch()
              }
              minHeight={430}
            />
          ) : barsQuery.data.payload.items.length === 0 ? (
            <MarketDataState
              variant="empty"
              title="该日期窗口没有 K 线"
              message="请调整起止日期；当前响应合同和数据版本有效。"
              minHeight={430}
            />
          ) : (
            <MarketKLineChart
              identity={`${scheme}:${sectorCode}`}
              period={period}
              bars={barsQuery.data.payload.items.map(
                /** 投影来源周期到 KLineChart，不改变数值或聚合粒度。 */
                (bar) => ({
                  date: bar.periodEnd,
                  open: bar.open,
                  high: bar.high,
                  low: bar.low,
                  close: bar.close,
                  volume: bar.volumeValue ?? undefined,
                  amount: bar.amountCny ?? undefined,
                }),
              )}
              height={430}
            />
          )}
        </CardContent>
      </Card>
      <Card component="section" aria-label="板块成分股">
        <CardContent>
          <Stack direction="row" justifyContent="space-between" alignItems="baseline">
            <Typography variant="h6">当前观察成分股</Typography>
            {constituentsQuery.data === undefined ? null : (
              <Typography variant="caption" color="text.secondary">
                解析至 {formatMarketDateTime(constituentsQuery.data.payload.release.resolvedAsOf)} ·
                身份覆盖 {constituentsQuery.data.payload.release.identityCoveragePercent}%
              </Typography>
            )}
          </Stack>
          <Alert severity="info" sx={{ mt: 1 }}>
            该列表是 verified 当前观察快照，不是指数行情，也不等同于正式历史调入调出记录。
          </Alert>
          {constituentsQuery.isPending ? (
            <Skeleton variant="rounded" height={300} sx={{ mt: 2 }} />
          ) : constituentsQuery.data === undefined ? (
            <MarketDataState
              variant="error"
              title="板块成分不可用"
              message="成分 publication 独立失败，不影响快照与 K 线。"
              onRetry={
                /** 仅重试板块成分 publication。 */
                () => void constituentsQuery.refetch()
              }
              minHeight={300}
            />
          ) : constituentsQuery.data.payload.items.length === 0 ? (
            <MarketDataState
              variant="empty"
              title="没有 verified 成分"
              message="publication 有效，但当前观察快照未返回可公开证券身份。"
              minHeight={300}
            />
          ) : (
            <>
              <TableContainer sx={{ mt: 1 }}>
                <Table size="small" aria-label="板块成分股">
                  <TableHead>
                    <TableRow>
                      <TableCell>股票</TableCell>
                      <TableCell>交易所</TableCell>
                      <TableCell>上市状态</TableCell>
                      <TableCell>观察区间</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {constituentsQuery.data.payload.items.map(
                      /** 成分行只深链真实证券身份承接页，不在本模块复制个股详情。 */
                      (item) => (
                        <TableRow key={`${item.exchange}:${item.symbol}`} hover>
                          <TableCell>
                            <Typography
                              component={RouterLink}
                              to={`/market/equities/${item.exchange}/${item.symbol}`}
                              variant="body2"
                              color="primary.main"
                              fontWeight={700}
                              sx={{ textDecoration: "none" }}
                            >
                              {item.name} · {item.symbol}
                            </Typography>
                          </TableCell>
                          <TableCell>{item.exchange}</TableCell>
                          <TableCell>{listingStatusLabel(item.listingStatus)}</TableCell>
                          <TableCell>
                            {formatMarketDateTime(item.observedFrom)} –{" "}
                            {item.observedTo === null
                              ? "当前"
                              : formatMarketDateTime(item.observedTo)}
                          </TableCell>
                        </TableRow>
                      ),
                    )}
                  </TableBody>
                </Table>
              </TableContainer>
              <Typography variant="caption" color="text.secondary">
                发布 {formatMarketDateTime(constituentsQuery.data.payload.release.publishedAt)} ·{" "}
                {constituentsQuery.data.payload.carriedForward ? "沿用最近观测" : "目标日观测"}
              </Typography>
            </>
          )}
        </CardContent>
      </Card>
    </Stack>
  );
}
