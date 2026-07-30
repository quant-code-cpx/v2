import { useEffect } from "react";
import type { ChangeEvent } from "react";
import {
  Alert,
  Box,
  Breadcrumbs,
  Card,
  CardContent,
  Chip,
  FormControl,
  InputLabel,
  Link,
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
  swIndustryBarsQueryOptions,
  swIndustryConstituentsQueryOptions,
  swIndustryResourceQueryOptions,
  swIndustryValuationQueryOptions,
} from "../../api/market";
import { MarketDataState } from "../../components/MarketDataState";
import { MarketKLineChart } from "../../components/MarketKLineChart";
import { MarketPageHeader } from "../../components/MarketPageHeader";
import { formatMarketDateTime, formatSourceDecimal } from "../../utils/market-formatters";

/** 返回上海时区今日日期，用于默认历史查询边界。 */
function shanghaiToday(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

/** 将日期向前平移自然日以形成默认日线窗口。 */
function subtractCalendarDays(date: string, days: number): string {
  const value = new Date(`${date}T00:00:00Z`);
  value.setUTCDate(value.getUTCDate() - days);
  return value.toISOString().slice(0, 10);
}

/** 格式化单字段估值状态，来源未报告时必须返回明确文案。 */
function valuationMetric(metric: {
  value: string | null;
  availability: "available" | "source_not_reported";
}): string {
  return metric.availability === "available" && metric.value !== null
    ? formatSourceDecimal(metric.value)
    : "来源未报告";
}

/** 将 URL 周期收敛为服务端已物化的申万 K 线周期。 */
function parsePeriod(value: string | null): "1d" | "1w" | "1mo" {
  return value === "1w" || value === "1mo" ? value : "1d";
}

/** 返回已物化申万 K 线周期的中文名称。 */
function periodLabel(period: "1d" | "1w" | "1mo"): string {
  if (period === "1w") return "周";
  if (period === "1mo") return "月";
  return "日";
}

/** 渲染单个申万节点的 taxonomy、逐字段估值、来源日线和正式成员。 */
export function SwIndustryDetailView() {
  const { code: rawCode = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const code = rawCode.trim();
  const canQuery = /^[0-9]{6}\.SI$/.test(code);
  const rawPeriod = searchParams.get("period");
  const period = parsePeriod(rawPeriod);
  const asOf = searchParams.get("asOf") ?? undefined;
  const defaultEnd = asOf ?? shanghaiToday();
  const end = searchParams.get("end") ?? defaultEnd;
  const start = searchParams.get("start") ?? subtractCalendarDays(end, 370);

  const resourceQuery = useQuery({
    ...swIndustryResourceQueryOptions(code || "000000.SI", asOf),
    enabled: canQuery,
  });
  const valuationQuery = useQuery({
    ...swIndustryValuationQueryOptions(code || "000000.SI", asOf),
    enabled: canQuery,
  });
  const barsQuery = useQuery({
    ...swIndustryBarsQueryOptions({
      code: code || "000000.SI",
      period,
      start,
      end,
      limit: 500,
    }),
    enabled: canQuery,
  });
  const constituentsQuery = useQuery({
    ...swIndustryConstituentsQueryOptions(code || "000000.SI", asOf),
    enabled: canQuery,
  });

  /** 删除非法或冗余周期参数，保证复制 URL 可复现规范查询。 */
  useEffect(() => {
    if (rawPeriod === null || rawPeriod === "1w" || rawPeriod === "1mo") return;
    const next = new URLSearchParams(searchParams);
    next.delete("period");
    setSearchParams(next, { replace: true });
  }, [rawPeriod, searchParams, setSearchParams]);

  /** 更新可分享 K 线周期或日期窗口。 */
  function updateChartUrl(key: "period" | "start" | "end", value: string): void {
    const next = new URLSearchParams(searchParams);
    if (key === "period" && value === "1d") next.delete(key);
    else next.set(key, value);
    setSearchParams(next);
  }

  /** 切换服务端已物化的申万 K 线周期。 */
  function handlePeriodChange(event: SelectChangeEvent): void {
    updateChartUrl("period", event.target.value);
  }

  /** 更新 K 线起始日期。 */
  function handleStartChange(event: ChangeEvent<HTMLInputElement>): void {
    updateChartUrl("start", event.target.value);
  }

  /** 更新 K 线结束日期。 */
  function handleEndChange(event: ChangeEvent<HTMLInputElement>): void {
    updateChartUrl("end", event.target.value);
  }

  /** 并行条件刷新四个独立 publication。 */
  function handleRefresh(): void {
    void Promise.all([
      resourceQuery.refetch(),
      valuationQuery.refetch(),
      barsQuery.refetch(),
      constituentsQuery.refetch(),
    ]);
  }

  if (!canQuery) {
    return (
      <Stack spacing={3}>
        <MarketPageHeader title="申万行业详情" subtitle="申万代码必须符合 000000.SI 格式。" />
        <MarketDataState
          variant="error"
          title="申万行业代码无效"
          message={`无法识别 ${code || "空代码"}。`}
          minHeight={360}
        />
      </Stack>
    );
  }

  const resource = resourceQuery.data?.payload;
  const valuation = valuationQuery.data?.payload;
  const industryName = resource?.industry.name ?? valuation?.industry.name ?? code;

  return (
    <Stack spacing={3}>
      <Breadcrumbs aria-label="申万 taxonomy 层级">
        <Link component={RouterLink} to="/market/industries/sw" underline="hover">
          申万行业
        </Link>
        {resource?.ancestors.map(
          /** 按服务端闭包顺序渲染真实父级，不通过代码段猜层级。 */
          (ancestor) => (
            <Link
              key={ancestor.code}
              component={RouterLink}
              to={`/market/industries/sw/${encodeURIComponent(ancestor.code)}`}
              underline="hover"
            >
              {ancestor.name}
            </Link>
          ),
        )}
        <Typography color="text.primary">{industryName}</Typography>
      </Breadcrumbs>
      <MarketPageHeader
        title={industryName}
        subtitle={`${code} · ${resource === undefined ? "申万节点" : `申万${resource.industry.level}级`}；与东财同名板块不作直接等价。`}
        status={
          valuation === undefined ? (
            <Chip size="small" variant="outlined" label="等待估值 publication" />
          ) : (
            <Chip
              size="small"
              color="success"
              variant="outlined"
              label={`${valuation.tradeDate} · final`}
            />
          )
        }
        onRefresh={handleRefresh}
        refreshing={
          resourceQuery.isFetching ||
          valuationQuery.isFetching ||
          barsQuery.isFetching ||
          constituentsQuery.isFetching
        }
      />
      {resourceQuery.isError ||
      valuationQuery.isError ||
      barsQuery.isError ||
      constituentsQuery.isError ? (
        <Alert severity="warning">
          部分 publication 读取失败；页面保留成功组件及其各自数据版本，不跨来源补齐失败字段。
        </Alert>
      ) : null}
      <Alert severity="info">
        taxonomy、估值、行情和正式成分分别发布；页面展示各自时间与版本，不把一次成功读取描述为统一实时快照。
      </Alert>
      {valuationQuery.isPending ? (
        <Skeleton variant="rounded" height={140} />
      ) : valuation === undefined ? (
        <MarketDataState
          variant="error"
          title="申万估值不可用"
          message="单节点估值 publication 未载入；PE、PB、PE_TTM 与股息率均不会补零。"
          onRetry={
            /** 仅重试单节点估值。 */
            () => void valuationQuery.refetch()
          }
        />
      ) : (
        <Box
          component="section"
          aria-label="申万行业估值"
          sx={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 2 }}
        >
          <Card>
            <CardContent>
              <Typography variant="caption" color="text.secondary">
                PE
              </Typography>
              <Typography variant="h6" sx={{ mt: 1 }}>
                {valuationMetric(valuation.valuation.pe)}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                来源字段 pe
              </Typography>
            </CardContent>
          </Card>
          <Card>
            <CardContent>
              <Typography variant="caption" color="text.secondary">
                PB
              </Typography>
              <Typography variant="h6" sx={{ mt: 1 }}>
                {valuationMetric(valuation.valuation.pb)}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                来源字段 pb
              </Typography>
            </CardContent>
          </Card>
          <Card>
            <CardContent>
              <Typography variant="caption" color="text.secondary">
                PE_TTM
              </Typography>
              <Typography variant="h6" sx={{ mt: 1 }}>
                {valuationMetric(valuation.valuation.peTtm)}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                当前来源未提供该字段
              </Typography>
            </CardContent>
          </Card>
          <Card>
            <CardContent>
              <Typography variant="caption" color="text.secondary">
                股息率
              </Typography>
              <Typography variant="h6" sx={{ mt: 1 }}>
                {valuationMetric(valuation.valuation.dividendYield)}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                当前来源未提供该字段
              </Typography>
            </CardContent>
          </Card>
        </Box>
      )}
      <Card component="section" aria-label={`申万行业${periodLabel(period)} K 线`}>
        <CardContent>
          <Stack direction="row" spacing={2} alignItems="center">
            <Typography variant="h6">申万行业{periodLabel(period)} K 线</Typography>
            <FormControl size="small" sx={{ minWidth: 112, ml: "auto" }}>
              <InputLabel id="sw-period-label">周期</InputLabel>
              <Select
                labelId="sw-period-label"
                value={period}
                label="周期"
                onChange={handlePeriodChange}
              >
                <MenuItem value="1d">日线</MenuItem>
                <MenuItem value="1w">周线</MenuItem>
                <MenuItem value="1mo">月线</MenuItem>
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
            {period === "1d"
              ? "日线 OHLCV 为来源直报，previousClose 按 close - change 写时派生。"
              : "周/月线由同步服务基于完整日线按自然周期边界写时物化，仅已结束周期标记 final；浏览器不聚合。"}
            {" 成交量单位：供应商原生单位，当前未证明单位，跨来源不可比且页面不换算。"}
          </Typography>
          {barsQuery.isPending ? (
            <Skeleton variant="rounded" height={430} sx={{ mt: 2 }} />
          ) : barsQuery.data === undefined ? (
            <MarketDataState
              variant="error"
              title={`申万${periodLabel(period)} K 线不可用`}
              message="已物化 K 线 publication 未载入；不会使用东财板块 K 线或浏览器临时聚合替代。"
              onRetry={
                /** 仅重试当前申万 K 线周期。 */
                () => void barsQuery.refetch()
              }
              minHeight={430}
            />
          ) : barsQuery.data.payload.items.length === 0 ? (
            <MarketDataState
              variant="empty"
              title={`该日期窗口没有${periodLabel(period)}线`}
              message="请调整起止日期。"
              minHeight={430}
            />
          ) : (
            <MarketKLineChart
              identity={`sw.industry:${code}`}
              period={period}
              bars={barsQuery.data.payload.items.map(
                /** 投影申万已物化周期到 KLineChart，不在浏览器改变聚合粒度。 */
                (bar) => ({
                  date: bar.periodEnd,
                  open: bar.open,
                  high: bar.high,
                  low: bar.low,
                  close: bar.close,
                  volume: bar.volume ?? undefined,
                  amount: bar.amountCny ?? undefined,
                }),
              )}
              height={430}
            />
          )}
        </CardContent>
      </Card>
      <Card component="section" aria-label="申万成员最新修订有效区间">
        <CardContent>
          <Stack direction="row" justifyContent="space-between" alignItems="baseline">
            <Typography variant="h6">成员最新修订有效区间</Typography>
            {constituentsQuery.data === undefined ? null : (
              <Typography variant="caption" color="text.secondary">
                口径日 {constituentsQuery.data.payload.snapshotDate} · 知识截点{" "}
                {formatMarketDateTime(constituentsQuery.data.payload.knowledgeCutoff)}
                {" · "}输入版本 {constituentsQuery.data.payload.inputDataVersions.length} 个
              </Typography>
            )}
          </Stack>
          <Alert severity="info" sx={{ mt: 1 }}>
            这是申万成员的最新修订有效区间，不是当时可知快照；in/out 是来源有效日期，observedAt
            是采集观察时间，二者不会互相冒充。
          </Alert>
          {constituentsQuery.data === undefined ? null : (
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
              {constituentsQuery.data.payload.source.provider} ·{" "}
              {constituentsQuery.data.payload.source.sourceDataset} · 观察于{" "}
              {formatMarketDateTime(constituentsQuery.data.payload.observedAt)} · 发布于{" "}
              {formatMarketDateTime(constituentsQuery.data.payload.publishedAt)}
            </Typography>
          )}
          {constituentsQuery.isPending ? (
            <Skeleton variant="rounded" height={300} sx={{ mt: 2 }} />
          ) : constituentsQuery.data === undefined ? (
            <MarketDataState
              variant="error"
              title="申万成员有效区间不可用"
              message="成员 publication 独立失败，不影响估值与 K 线。"
              onRetry={
                /** 仅重试申万成员有效区间。 */
                () => void constituentsQuery.refetch()
              }
              minHeight={300}
            />
          ) : constituentsQuery.data.payload.items.length === 0 ? (
            <MarketDataState
              variant="empty"
              title="当前没有成员记录"
              message="publication 有效，但当前口径日没有返回成员有效区间。"
              minHeight={300}
            />
          ) : (
            <TableContainer sx={{ mt: 1 }}>
              <Table size="small" aria-label="申万成员最新修订有效区间">
                <TableHead>
                  <TableRow>
                    <TableCell>股票</TableCell>
                    <TableCell>交易所</TableCell>
                    <TableCell>成员状态</TableCell>
                    <TableCell>最新修订有效区间</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {constituentsQuery.data.payload.items.map(
                    /** 证券身份深链到 0007 承接页，本模块不重复设计个股详情。 */
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
                        <TableCell>口径日有效</TableCell>
                        <TableCell>
                          起 {item.inDate ?? "来源未报告"} · 至 {item.outDate ?? "尚无结束日期"}
                        </TableCell>
                      </TableRow>
                    ),
                  )}
                </TableBody>
              </Table>
            </TableContainer>
          )}
        </CardContent>
      </Card>
      <Typography variant="caption" color="text.secondary">
        {valuation === undefined
          ? "估值版本不可用"
          : `估值版本 ${valuation.dataVersion} · ${valuation.methodology.id} v${valuation.methodology.version} · 输入版本 ${valuation.inputDataVersions.length} 个`}
        {" · "}
        {barsQuery.data === undefined
          ? "K 线版本不可用"
          : `K 线版本 ${barsQuery.data.payload.dataVersion} · ${barsQuery.data.payload.methodology.id} v${barsQuery.data.payload.methodology.version} · 成交量单位 ${barsQuery.data.payload.volumeUnit} · 输入版本 ${barsQuery.data.payload.inputDataVersions.length} 个`}
        {" · "}
        {constituentsQuery.data === undefined
          ? "成员版本不可用"
          : `成员版本 ${constituentsQuery.data.payload.dataVersion} · ${constituentsQuery.data.payload.historyMode}`}
      </Typography>
    </Stack>
  );
}
